import os
import json
import logging
from flask import Flask, request, render_template, jsonify
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, template_folder="../templates")

# Corrige o esquema de URL (http vs https) quando rodando atrás do proxy da Vercel
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Configuração de Logging
logging.basicConfig(level=logging.INFO)
# Silencia avisos internos do googleapiclient sobre cache
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

def get_credentials_from_request():
    """
    Extrai o token do cabeçalho Authorization e cria as credenciais.
    Espera formato: 'Bearer <token>'
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return None
    
    try:
        token = auth_header.split(' ')[1]
        # Cria credenciais apenas com o token de acesso (sem refresh token)
        # Isso funciona porque o Firebase no frontend garante que o token é recente.
        return Credentials(token=token)
    except IndexError:
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

@app.route('/get_video_info', methods=['POST'])
def get_video_info():
    creds = get_credentials_from_request()
    if not creds:
        return jsonify({"status": "error", "message": "Faça login primeiro."}), 401
    
    data = request.get_json()
    video_id = data.get('video_id')
    
    if not video_id:
        return jsonify({"status": "error", "message": "ID inválido."}), 400

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

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
    creds = get_credentials_from_request()
    if not creds:
        return jsonify({"status": "error", "message": "Faça login primeiro."}), 401
    
    query = request.args.get('q')
    if not query:
        return jsonify({"status": "success", "channels": []})

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

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
    creds = get_credentials_from_request()
    if not creds:
        return jsonify({"status": "error", "message": "Faça login primeiro."}), 401

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    
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
                                maxResults=3
                            ).execute()

                            for vid_item in pl_resp.get("items", []):
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
                    part="snippet,liveStreamingDetails",
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
                        elif details['snippet'].get('liveBroadcastContent') == 'upcoming':
                            is_live = True
                            v['type'] = 'EM BREVE 🕒'
                            
                        if is_live:
                            # Pega a contagem de espectadores se disponível
                            if 'liveStreamingDetails' in details:
                                viewers = details['liveStreamingDetails'].get('concurrentViewers')
                                if viewers:
                                    v['viewers'] = viewers
                    
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
    # O frontend agora gerencia o estado de login via Firebase
    return render_template('index.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/send', methods=['POST'])
def send_message():
    # Tenta obter dados do JSON (se enviado via fetch/axios) ou do Form Data
    data = request.get_json(silent=True) or request.form

    # Fallback: Se o frontend enviou JSON mas esqueceu o header Content-Type
    if not data and request.data:
        try:
            data = json.loads(request.data)
        except Exception:
            pass

    video_id = data.get('video_id')
    message = data.get('message')
    msg_type = data.get('type') # 'comment' ou 'live'

    # Log para debug na Vercel (verifique o que está chegando)
    logger.info(f"Payload recebido em /send: {data}")

    if not video_id or not message:
        return jsonify({"status": "error", "message": "Faltam dados."}), 400

    creds = get_credentials_from_request()
    if not creds:
        return jsonify({"status": "error", "message": "Usuário não logado."}), 401

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

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
        try:
            error_content = json.loads(e.content)
            reason = error_content.get('error', {}).get('errors', [{}])[0].get('reason', 'Unknown')
            msg = f"Erro API: {reason}"
        except Exception:
            msg = f"Erro API: {e}"
        return jsonify({"status": "error", "message": msg}), e.resp.status
    except RefreshError:
        return jsonify({"status": "error", "message": "Sessão expirada. Faça login novamente."}), 401
    except Exception as e:
        logger.exception("Erro inesperado no envio")
        return jsonify({"status": "error", "message": str(e)}), 500

# Necessário para rodar localmente se quiser testar
if __name__ == '__main__':
    app.run(debug=True)