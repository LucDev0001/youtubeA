import os
import json
import logging
from flask import Flask, request, render_template, jsonify, session, redirect, url_for
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, template_folder="../templates")

# Corrige o esquema de URL (http vs https) quando rodando atrás do proxy da Vercel
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# IMPORTANTE: Defina uma chave secreta para assinar os cookies da sessão
# Na Vercel, você deve definir isso nas Environment Variables como FLASK_SECRET_KEY
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "uma_chave_secreta_padrao_para_dev")

# Permite HTTP para testes locais (OAuthlib reclama se não for HTTPS)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Configuração de Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# Tenta carregar as credenciais do cliente (Client ID/Secret)
# Na Vercel, coloque o conteúdo do client_secret.json na variável CLIENT_SECRETS_JSON
CLIENT_SECRETS_JSON = os.environ.get("CLIENT_SECRETS_JSON")
if not CLIENT_SECRETS_JSON and os.path.exists("client_secret.json"):
    with open("client_secret.json", "r") as f:
        CLIENT_SECRETS_JSON = f.read()

def credentials_to_dict(credentials):
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

@app.route('/login')
def login():
    if not CLIENT_SECRETS_JSON:
        return "Erro: CLIENT_SECRETS_JSON não configurado no servidor.", 500

    client_config = json.loads(CLIENT_SECRETS_JSON)
    
    # Cria o fluxo OAuth
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES
    )
    # Define para onde o Google deve redirecionar após o login
    flow.redirect_uri = url_for('oauth2callback', _external=True)
    
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )
    session['state'] = state
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = session['state']
    client_config = json.loads(CLIENT_SECRETS_JSON)
    
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        state=state
    )
    flow.redirect_uri = url_for('oauth2callback', _external=True)
    
    # Troca o código de autorização por credenciais
    flow.fetch_token(authorization_response=request.url)
    
    # Salva as credenciais na sessão do usuário
    session['credentials'] = credentials_to_dict(flow.credentials)
    return redirect(url_for('home'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

def get_live_chat_id(youtube, video_id):
    try:
        response = youtube.videos().list(part="liveStreamingDetails", id=video_id).execute()
        items = response.get("items", [])
        if not items: return None
        live_details = items[0].get("liveStreamingDetails")
        return live_details.get("activeLiveChatId") if live_details else None
    except HttpError:
        return None

@app.route('/get_video_info', methods=['POST'])
def get_video_info():
    if 'credentials' not in session:
        return jsonify({"status": "error", "message": "Faça login primeiro."}), 401
    
    data = request.get_json()
    video_id = data.get('video_id')
    
    if not video_id:
        return jsonify({"status": "error", "message": "ID inválido."}), 400

    creds = Credentials(**session['credentials'])
    youtube = build("youtube", "v3", credentials=creds)

    try:
        response = youtube.videos().list(
            part="snippet,liveStreamingDetails",
            id=video_id
        ).execute()

        items = response.get("items", [])
        if not items:
            return jsonify({"status": "error", "message": "Vídeo não encontrado."}), 404

        snippet = items[0]["snippet"]
        live_details = items[0].get("liveStreamingDetails", {})
        
        return jsonify({
            "status": "success",
            "title": snippet["title"],
            "channel": snippet["channelTitle"],
            "thumbnail": snippet["thumbnails"]["medium"]["url"],
            "is_live": bool(live_details.get("activeLiveChatId"))
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/')
def home():
    is_logged_in = 'credentials' in session
    return render_template('index.html', logged_in=is_logged_in)

@app.route('/send', methods=['POST'])
def send_message():
    video_id = request.form.get('video_id')
    message = request.form.get('message')
    msg_type = request.form.get('type') # 'comment' ou 'live'

    if not video_id or not message:
        return jsonify({"status": "error", "message": "Faltam dados."}), 400

    if 'credentials' not in session:
        return jsonify({"status": "error", "message": "Usuário não logado."}), 401

    # Reconstrói as credenciais a partir da sessão
    creds = Credentials(**session['credentials'])
    youtube = build("youtube", "v3", credentials=creds)

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
    app.run(debug=False)