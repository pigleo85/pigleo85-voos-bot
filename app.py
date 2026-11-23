import os
import json
import io
import requests
from flask import Flask, request
from PIL import Image
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# Variáveis de Ambiente
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")

SPREADSHEET_NAME = "Voos Embarque x Desembarque"

class TrechoVoo(BaseModel):
    data_voo: str = Field(description="Data do voo no formato DD/MM/AAAA")
    trajeto: str = Field(description="Origem e destino IATA, ex: GIG x VIX ou VIX x GIG")
    cia: str = Field(description="Companhia aérea em maiúsculo, ex: GOL, LATAM, AZUL")
    horario_partida: str = Field(description="Horário de partida HH:MM, ex: 19:15")
    localizador: str = Field(description="Código localizador de 6 caracteres, ex: ELRBTE")
    preco_total: float = Field(description="Valor total pago em reais incluindo taxas, ex: 247.01")
    obs: str = Field(description="Quantidade de milhas se resgatado com pontos (ex: '10600 milhas'), ou string vazia ''")

class ComprovantePassagem(BaseModel):
    voos: list[TrechoVoo]

def get_sheets_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

def baixar_arquivo_telegram(file_id):
    file_info = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
    ).json()
    file_path = file_info["result"]["file_path"]
    return requests.get(
        f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    ).content

def extrair_dados_comprovante(file_bytes, mime_type):
    client = genai.Client(api_key=GEMINI_API_KEY)

    if mime_type == "application/pdf":
        conteudo_midia = types.Part.from_bytes(data=file_bytes, mime_type="application/pdf")
    else:
        conteudo_midia = Image.open(io.BytesIO(file_bytes))

    prompt = (
        "Analise este comprovante de compra de passagem aérea (imagem ou PDF). "
        "Extraia todos os trechos de voo presentes no documento. "
        "Atenção: pegue a DATA DO VOO (embarque), nunca a data da compra/emissão. "
        "Se houver ida e volta no mesmo comprovante e apenas um valor total pago, "
        "coloque o valor total no primeiro trecho e 0.0 no segundo."
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[conteudo_midia, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ComprovantePassagem,
            temperature=0.1,
        ),
    )
    return json.loads(response.text)

def atualizar_planilha(voos):
    gc = get_sheets_client()
    sh = gc.open(SPREADSHEET_NAME)
    resultados = []

    for voo in voos:
        data_alvo = voo["data_voo"].strip()
        ano = data_alvo.split("/")[-1]

        abas_para_buscar = [ano]
        if ano == "2028":
            abas_para_buscar.append("2027")

        encontrado = False
        for nome_aba in abas_para_buscar:
            try:
                ws = sh.worksheet(nome_aba)
            except gspread.WorksheetNotFound:
                continue

            datas_coluna = ws.col_values(1)  # Coluna A = Data

            linha_escolhida = None
            for idx, val in enumerate(datas_coluna, start=1):
                if val.strip() == data_alvo:
                    status_atual = ws.cell(idx, 2).value or ""
                    linha_escolhida = idx
                    if status_atual.strip().lower() == "pendente":
                        break

            if linha_escolhida:
                preco_formatado = (
                    f"R$ {voo['preco_total']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    if voo["preco_total"] > 0
                    else ""
                )

                ws.update(
                    range_name=f"B{linha_escolhida}:H{linha_escolhida}",
                    values=[[
                        "Comprada",
                        voo["trajeto"],
                        voo["cia"],
                        voo["horario_partida"],
                        voo["localizador"].upper(),
                        preco_formatado,
                        voo["obs"]
                    ]],
                    value_input_option="USER_ENTERED"
                )
                resultados.append(
                    f"✅ *Aba {nome_aba} (Linha {linha_escolhida}) atualizada!*\n"
                    f"📅 *Data:* {data_alvo} ({voo['trajeto']})\n"
                    f"✈️ *Voo:* {voo['cia']} às {voo['horario_partida']} | *Loc:* `{voo['localizador'].upper()}`\n"
                    f"💰 *Valor:* {preco_formatado or 'Incluso na ida'}"
                )
                encontrado = True
                break

        if not encontrado:
            resultados.append(f"⚠️ Data *{data_alvo}* não encontrada na planilha. Nenhuma linha alterada.")

    return "\n\n".join(resultados)

@app.route("/", methods=["GET"])
def health():
    return "Bot de Voos ativo!", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return "OK", 200

    msg = data["message"]
    chat_id = str(msg["chat"]["id"])

    if ALLOWED_CHAT_ID and chat_id != str(ALLOWED_CHAT_ID):
        return "Unauthorized", 200

    # Caso 1: Print enviado como foto
    if "photo" in msg:
        send_telegram_message(chat_id, "⏳ Lendo print da passagem...")
        try:
            file_id = msg["photo"][-1]["file_id"]
            file_bytes = baixar_arquivo_telegram(file_id)
            dados = extrair_dados_comprovante(file_bytes, "image/jpeg")
            resposta = atualizar_planilha(dados.get("voos", []))
            send_telegram_message(chat_id, resposta)
        except Exception as e:
            send_telegram_message(chat_id, f"❌ Erro ao processar print: `{str(e)}`")

    # Caso 2: Arquivo PDF ou imagem enviada sem compressão
    elif "document" in msg:
        doc = msg["document"]
        mime_type = doc.get("mime_type", "")
        if mime_type == "application/pdf" or mime_type.startswith("image/"):
            send_telegram_message(chat_id, "⏳ Lendo comprovante em arquivo...")
            try:
                file_bytes = baixar_arquivo_telegram(doc["file_id"])
                dados = extrair_dados_comprovante(file_bytes, mime_type)
                resposta = atualizar_planilha(dados.get("voos", []))
                send_telegram_message(chat_id, resposta)
            except Exception as e:
                send_telegram_message(chat_id, f"❌ Erro ao processar arquivo: `{str(e)}`")
        else:
            send_telegram_message(chat_id, "⚠️ Formato não suportado. Envie uma *foto/print* ou *PDF* da passagem.")

    else:
        send_telegram_message(chat_id, "📸 Envie o *print* ou o *PDF* da compra da passagem para atualizar a planilha.")

    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
