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

@app.route('/search_channels', methods=['GET'])
def search_channels():
    if 'credentials' not in session:
        return jsonify({"status": "error", "message": "Faça login primeiro."}), 401
    
    query = request.args.get('q')
    if not query:
        return jsonify({"status": "success", "channels": []})

    creds = Credentials(**session['credentials'])
    youtube = build("youtube", "v3", credentials=creds)

    try:
        resp = youtube.search().list(
            part="snippet",
            type="channel",
            q=query,
            maxResults=5
        ).execute()
        
        channels = []
        for item in resp.get("items", []):
            channels.append({
                "id": item["snippet"]["channelId"],
                "title": item["snippet"]["channelTitle"],
                "thumbnail": item["snippet"]["thumbnails"]["default"]["url"]
            })
        return jsonify({"status": "success", "channels": channels})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get_recent_videos', methods=['GET'])
def get_recent_videos():
    if 'credentials' not in session:
        return jsonify({"status": "error", "message": "Faça login primeiro."}), 401

    creds = Credentials(**session['credentials'])
    youtube = build("youtube", "v3", credentials=creds)
    
    page_token = request.args.get('pageToken')
    channel_filter = request.args.get('channelId')
    live_only = request.args.get('liveOnly') == 'true'

    try:
        videos = []
        next_page_token = None

        if channel_filter:
            # MODO 1: Vídeos de um canal específico
            # Primeiro pega o ID da playlist de uploads
            ch_resp = youtube.channels().list(
                part="contentDetails",
                id=channel_filter
            ).execute()
            
            if ch_resp.get("items"):
                uploads_id = ch_resp["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
                
                pl_resp = youtube.playlistItems().list(
                    part="snippet",
                    playlistId=uploads_id,
                    maxResults=12,
                    pageToken=page_token
                ).execute()
                
                next_page_token = pl_resp.get("nextPageToken")
                
                for item in pl_resp.get("items", []):
                    videos.append({
                        "id": item["snippet"]["resourceId"]["videoId"],
                        "title": item["snippet"]["title"],
                        "channel": item["snippet"]["channelTitle"],
                        "thumbnail": item["snippet"]["thumbnails"]["medium"]["url"],
                        "type": "VÍDEO"
                    })
        else:
            # MODO 2: Feed de Inscrições (Padrão)
            
            # 1. Buscar Inscrições (Canais que o usuário segue)
            subs_resp = youtube.subscriptions().list(
                part="snippet",
                mine=True,
                maxResults=6,
                order="relevance",
                pageToken=page_token
            ).execute()
            
            next_page_token = subs_resp.get("nextPageToken")

            sub_channels = []
            for item in subs_resp.get("items", []):
                sub_channels.append({
                    "id": item["snippet"]["resourceId"]["channelId"],
                    "title": item["snippet"]["title"]
                })

            if sub_channels:
                # 2. Buscar ID da playlist de Uploads desses canais
                channel_ids = [ch["id"] for ch in sub_channels]
                channels_resp = youtube.channels().list(
                    part="contentDetails",
                    id=",".join(channel_ids)
                ).execute()

                uploads_map = {}
                for item in channels_resp.get("items", []):
                    uploads_map[item["id"]] = item["contentDetails"]["relatedPlaylists"]["uploads"]

                # 3. Buscar o vídeo mais recente de cada canal
                for ch in sub_channels:
                    pid = uploads_map.get(ch["id"])
                    if pid:
                        try:
                            pl_resp = youtube.playlistItems().list(
                                part="snippet",
                                playlistId=pid,
                                maxResults=1
                            ).execute()

                            if pl_resp.get("items"):
                                vid_item = pl_resp["items"][0]
                                videos.append({
                                    "id": vid_item["snippet"]["resourceId"]["videoId"],
                                    "title": vid_item["snippet"]["title"],
                                    "channel": ch["title"],
                                    "thumbnail": vid_item["snippet"]["thumbnails"]["medium"]["url"],
                                    "type": "NOVO"
                                })
                        except Exception:
                            continue

        # Enriquecer dados para verificar se é LIVE real e filtrar
        if videos:
            video_ids = [v['id'] for v in videos]
            try:
                # Consulta detalhes para saber se é live
                vid_resp = youtube.videos().list(
                    part="snippet",
                    id=",".join(video_ids)
                ).execute()
                
                vid_map = {item['id']: item for item in vid_resp.get('items', [])}
                final_videos = []

                for v in videos:
                    details = vid_map.get(v['id'])
                    is_live = False
                    
                    if details:
                        if details['snippet'].get('liveBroadcastContent') == 'live':
                            is_live = True
                            v['type'] = 'LIVE 🔴'
                    
                    if live_only and not is_live:
                        continue
                    
                    final_videos.append(v)
                videos = final_videos
            except Exception as e:
                logger.error(f"Erro ao verificar status de live: {e}")

        return jsonify({"status": "success", "videos": videos, "nextPageToken": next_page_token})

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