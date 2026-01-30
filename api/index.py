import os
import json
import logging
from flask import Flask, request, render_template, jsonify
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

app = Flask(__name__, template_folder="../templates")

# Configuração de Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

def get_authenticated_service():
    """
    No Vercel, lemos o token da Variável de Ambiente 'GOOGLE_TOKEN_JSON'.
    Isso evita ter que fazer login via navegador no servidor.
    """
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    
    if not token_json:
        logger.error("Variável de ambiente GOOGLE_TOKEN_JSON não encontrada.")
        return None

    try:
        info = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(info, SCOPES)
        return build("youtube", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Erro na autenticação: {e}")
        return None

def get_live_chat_id(youtube, video_id):
    try:
        response = youtube.videos().list(part="liveStreamingDetails", id=video_id).execute()
        items = response.get("items", [])
        if not items: return None
        live_details = items[0].get("liveStreamingDetails")
        return live_details.get("activeLiveChatId") if live_details else None
    except HttpError:
        return None

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/send', methods=['POST'])
def send_message():
    video_id = request.form.get('video_id')
    message = request.form.get('message')
    msg_type = request.form.get('type') # 'comment' ou 'live'

    if not video_id or not message:
        return jsonify({"status": "error", "message": "Faltam dados."}), 400

    youtube = get_authenticated_service()
    if not youtube:
        return jsonify({"status": "error", "message": "Erro de Autenticação no Servidor."}), 500

    try:
        if msg_type == 'live':
            chat_id = get_live_chat_id(youtube, video_id)
            if not chat_id:
                return jsonify({"status": "error", "message": "Chat ao vivo não encontrado."}), 404
            
            youtube.liveChatMessages().insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": chat_id,
                        "type": "textMessageEvent",
                        "textMessageDetails": {"messageText": message}
                    }
                }
            ).execute()
            return jsonify({"status": "success", "message": "Mensagem enviada na Live!"})

        else: # Comentário Normal
            youtube.commentThreads().insert(
                part="snippet",
                body={
                    "snippet": {
                        "videoId": video_id,
                        "topLevelComment": {
                            "snippet": {"textOriginal": message}
                        }
                    }
                }
            ).execute()
            return jsonify({"status": "success", "message": "Comentário postado!"})

    except HttpError as e:
        error_content = json.loads(e.content)
        reason = error_content.get('error', {}).get('errors', [{}])[0].get('reason', 'Unknown')
        return jsonify({"status": "error", "message": f"Erro API: {reason}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# Necessário para rodar localmente se quiser testar
if __name__ == '__main__':
    app.run(debug=True)