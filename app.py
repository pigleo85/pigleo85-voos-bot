import json
import os
import re
import time
import requests
from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types
from google.genai.errors import APIError

app = Flask(__name__)

TELEGRAM_API_BASE = "https:" + "//api.telegram.org"


def _limpar_token(token_bruto):
    if not token_bruto:
        return ""
    match = re.search(r"\d+:[A-Za-z0-9_-]+", str(token_bruto))
    return match.group(0) if match else str(token_bruto).strip()


TELEGRAM_TOKEN = _limpar_token(os.environ.get("TELEGRAM_TOKEN", ""))
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY", "") or "").strip().strip('"').strip("'")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
SHEET_NAME = (os.environ.get("SHEET_NAME", "Voos Embarque x Desembarque") or "Voos Embarque x Desembarque").strip()
ALLOWED_CHAT_ID = (os.environ.get("ALLOWED_CHAT_ID", "") or "").strip()

MODELOS_GEMINI = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
]

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()


def enviar_mensagem(chat_id, texto):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texto,
    }
    try:
        requests.post(url, json=payload, timeout=20)
    except Exception:
        pass


def obter_planilha():
    if not GOOGLE_CREDENTIALS_JSON:
        raise ValueError("Variável GOOGLE_CREDENTIALS_JSON não configurada.")
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https:" + "//www.googleapis.com/auth/spreadsheets",
        "https:" + "//www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open(SHEET_NAME)


def _extrair_retry_delay(msg):
    match = re.search(r"retry in ([0-9.]+)s", msg, re.IGNORECASE)
    if match:
        try:
            segundos = float(match.group(1))
            horas = int(segundos // 3600)
            minutos = int((segundos % 3600) // 60)
            if horas > 0:
                return f"{horas}h {minutos}min"
            if minutos > 0:
                return f"{minutos}min"
            return f"{int(segundos)}s"
        except ValueError:
            pass
    return None


class QuotaExcedidaError(Exception):
    pass


def extrair_dados_comprovante(file_bytes, mime_type):
    prompt = (
        "Analise este comprovante ou passagem aérea e extraia os dados do voo em JSON puro, "
        "sem blocos markdown, com as chaves: "
        '{"companhia": "", "numero_voo": "", "origem": "", "destino": "", '
        '"data_embarque": "", "hora_embarque": "", "data_chegada": "", "hora_chegada": "", '
        '"localizador": "", "passageiro": ""}. '
        "Se algum campo não existir, use string vazia."
    )

    parte_arquivo = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
    ultimo_erro = None
    teve_quota = False

    for modelo in MODELOS_GEMINI:
        for tentativa in range(4):
            try:
                response = client.models.generate_content(
                    model=modelo,
                    contents=[parte_arquivo, prompt],
                )
                texto = (response.text or "").strip()
                if texto.startswith("```"):
                    linhas = texto.splitlines()
                    if len(linhas) >= 3:
                        texto = "\n".join(linhas[1:-1]).strip()
                return json.loads(texto), modelo
            except APIError as e:
                ultimo_erro = e
                msg = str(e)
                codigo = getattr(e, "code", None)
                if codigo == 429 or "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    teve_quota = True
                    tempo_espera = _extrair_retry_delay(msg)
                    aviso = f"Quota da API Gemini atingida no modelo {modelo} (429 RESOURCE_EXHAUSTED)."
                    if tempo_espera:
                        aviso += f" Aguarde cerca de {tempo_espera} para nova tentativa."
                    ultimo_erro = QuotaExcedidaError(aviso)
                    break
                if codigo == 404 or "404" in msg or "NOT_FOUND" in msg:
                    break
                if codigo == 503 or "503" in msg or "UNAVAILABLE" in msg:
                    time.sleep(3 * (tentativa + 1))
                    continue
                raise
            except Exception as e:
                ultimo_erro = e
                time.sleep(1)

    if teve_quota and isinstance(ultimo_erro, QuotaExcedidaError):
        raise ultimo_erro
    raise ultimo_erro if ultimo_erro else RuntimeError("Falha ao consultar o Gemini.")


def registrar_na_planilha(dados):
    planilha = obter_planilha()
    aba = planilha.sheet1
    linha = [
        dados.get("data_embarque", ""),
        dados.get("hora_embarque", ""),
        dados.get("origem", ""),
        dados.get("destino", ""),
        dados.get("companhia", ""),
        dados.get("numero_voo", ""),
        dados.get("localizador", ""),
        dados.get("passageiro", ""),
        dados.get("data_chegada", ""),
        dados.get("hora_chegada", ""),
    ]
    aba.append_row(linha, value_input_option="USER_ENTERED")
    return aba.title, len(aba.get_all_values())


def baixar_arquivo_telegram(file_id):
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN inválido ou vazio.")
    info_url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_TOKEN}/getFile"
    info_resp = requests.get(info_url, params={"file_id": file_id}, timeout=20)
    info_resp.raise_for_status()
    info_json = info_resp.json()
    file_path = info_json["result"]["file_path"].lstrip("/")
    file_url = f"{TELEGRAM_API_BASE}/file/bot{TELEGRAM_TOKEN}/{file_path}"
    file_resp = requests.get(file_url, timeout=30)
    file_resp.raise_for_status()
    return file_resp.content, file_path


@app.route("/", methods=["GET", "HEAD"])
def home():
    return "Bot de Voos ativo!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    mensagem = data.get("message") or data.get("edited_message")
    if not mensagem:
        return jsonify({"ok": True}), 200

    chat_id = str(mensagem.get("chat", {}).get("id", ""))
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        return jsonify({"ok": True}), 200

    file_id = None
    mime_type = None

    if mensagem.get("photo"):
        file_id = mensagem["photo"][-1]["file_id"]
        mime_type = "image/jpeg"
        enviar_mensagem(chat_id, "⏳ Lendo print da passagem...")
    elif mensagem.get("document"):
        doc = mensagem["document"]
        file_id = doc["file_id"]
        mime_type = doc.get("mime_type") or "application/pdf"
        enviar_mensagem(chat_id, "⏳ Lendo arquivo da passagem...")
    else:
        enviar_mensagem(chat_id, "Envie um print ou PDF da passagem para registrar na planilha.")
        return jsonify({"ok": True}), 200

    try:
        file_bytes, _ = baixar_arquivo_telegram(file_id)
        dados, modelo_usado = extrair_dados_comprovante(file_bytes, mime_type)
        aba_titulo, num_linha = registrar_na_planilha(dados)
        resumo = (
            f"✅ Voo registrado com sucesso (modelo: {modelo_usado})\n"
            f"Aba: {aba_titulo} | Linha: {num_linha}\n"
            f"Origem: {dados.get('origem', '-')} → Destino: {dados.get('destino', '-')}\n"
            f"Embarque: {dados.get('data_embarque', '-')} {dados.get('hora_embarque', '')}\n"
            f"Voo: {dados.get('companhia', '-')} {dados.get('numero_voo', '')}"
        )
        enviar_mensagem(chat_id, resumo)
    except QuotaExcedidaError as e:
        enviar_mensagem(chat_id, f"⚠️ {e}")
    except Exception as e:
        enviar_mensagem(chat_id, f"❌ Erro ao processar print: {e}")

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
