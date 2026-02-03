import os
import json
import logging
import datetime
import random
import requests
from flask import Flask, request, render_template, jsonify, session, redirect, url_for, send_from_directory
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError
from werkzeug.middleware.proxy_fix import ProxyFix
import firebase_admin
from firebase_admin import credentials, firestore, auth

app = Flask(__name__, template_folder="../templates", static_folder="../static")

# Corrige o esquema de URL (http vs https) quando rodando atrás do proxy da Vercel
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# IMPORTANTE: Defina uma chave secreta para assinar os cookies da sessão
# Na Vercel, você deve definir isso nas Environment Variables como FLASK_SECRET_KEY
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "uma_chave_secreta_padrao_para_dev")

# Permite HTTP para testes locais (OAuthlib reclama se não for HTTPS)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Configuração de Logging
logging.basicConfig(level=logging.INFO)
# Silencia avisos internos do googleapiclient sobre cache
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

# --- CONFIGURAÇÃO FIREBASE ADMIN (BACKEND) ---
# Na Vercel, coloque o JSON da conta de serviço na variável FIREBASE_SERVICE_ACCOUNT
firebase_creds_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if firebase_creds_json:
    cred = credentials.Certificate(json.loads(firebase_creds_json))
    firebase_admin.initialize_app(cred)
    db = firestore.client()
else:
    # Fallback para desenvolvimento local se o arquivo existir
    if os.path.exists("firebase_service_account.json"):
        cred = credentials.Certificate("firebase_service_account.json")
        firebase_admin.initialize_app(cred)
        db = firestore.client()

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# --- ROTAÇÃO DE CHAVES API ---
# Carrega uma lista de credenciais ou um único arquivo.
# Para rotação, a variável de ambiente CLIENT_SECRETS_JSON pode ser uma lista de JSONs strings: ['{...}', '{...}']
raw_secrets = os.environ.get("CLIENT_SECRETS_JSON")
CLIENT_SECRETS_LIST = []

if raw_secrets:
    try:
        # Tenta carregar como lista de JSONs
        parsed = json.loads(raw_secrets)
        if isinstance(parsed, list):
            CLIENT_SECRETS_LIST = [json.dumps(p) if isinstance(p, dict) else p for p in parsed]
        else:
            CLIENT_SECRETS_LIST = [raw_secrets]
    except json.JSONDecodeError:
        CLIENT_SECRETS_LIST = [raw_secrets]
elif os.path.exists("client_secret.json"):
    with open("client_secret.json", "r") as f:
        CLIENT_SECRETS_LIST = [f.read()]

def credentials_to_dict(credentials):
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

def get_user_from_token():
    """Verifica o token do Firebase enviado no Header Authorization"""
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split("Bearer ")[1]
    try:
        decoded_token = auth.verify_id_token(token)
        return decoded_token # Retorna dict com 'uid', 'email', etc.
    except Exception as e:
        logger.error(f"Erro auth firebase: {e}")
        return None

def get_youtube_service(uid):
    """Recupera credenciais do Firestore e cria o serviço do YouTube"""
    doc_ref = db.collection('users').document(uid)
    doc = doc_ref.get()
    if not doc.exists:
        return None
    
    user_data = doc.to_dict()
    yt_creds = user_data.get('youtube_credentials')
    if not yt_creds:
        return None
        
    creds = Credentials(**yt_creds)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)

@app.route('/connect_youtube')
def connect_youtube():
    """Inicia o fluxo OAuth para conectar o canal ao SaaS"""
    # O UID do usuário deve vir via query param ou sessão temporária antes do redirect
    # Para simplificar, vamos assumir que o frontend manda o usuário para cá
    # e depois no callback associamos.
    
    if not CLIENT_SECRETS_LIST:
        return "Erro: CLIENT_SECRETS_JSON não configurado no servidor.", 500

    # ROTAÇÃO: Escolhe um projeto aleatório para iniciar o fluxo
    selected_secret = random.choice(CLIENT_SECRETS_LIST)
    
    # Salva qual secret foi usado na sessão para usar no callback
    session['selected_secret_config'] = selected_secret
    client_config = json.loads(selected_secret)
    
    # Cria o fluxo OAuth
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES
    )
    # Define para onde o Google deve redirecionar após o login
    redirect_uri = url_for('oauth2callback', _external=True)
    flow.redirect_uri = redirect_uri
    
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )
    session['state'] = state
    
    # Guardamos o UID na sessão para saber quem está conectando
    user_uid = request.args.get('uid')
    if user_uid: session['connect_uid'] = user_uid
    
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')
    if not state:
        return redirect(url_for('login'))
    
    user_uid = session.get('connect_uid')
    selected_secret = session.get('selected_secret_config')

    if not selected_secret:
        return "Erro: Configuração de OAuth perdida na sessão.", 400

    client_config = json.loads(selected_secret)
    
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        state=state
    )
    flow.redirect_uri = url_for('oauth2callback', _external=True)
    
    try:
        # Troca o código de autorização por credenciais
        flow.fetch_token(authorization_response=request.url)
        
        creds_dict = credentials_to_dict(flow.credentials)
        
        # SALVA NO FIRESTORE
        if user_uid:
            db.collection('users').document(user_uid).set({
                'youtube_credentials': creds_dict,
                'youtube_connected': True
            }, merge=True)
            
        return redirect(url_for('home')) # Redireciona para o painel
    except Exception as e:
        logger.error(f"Erro no callback OAuth: {e}")
        return f"Erro ao fazer login: {str(e)} <br><a href='/'>Tentar novamente</a>"

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
    user = get_user_from_token()
    if not user: return jsonify({"status": "error", "message": "Não autenticado."}), 401
    
    data = request.get_json()
    video_id = data.get('video_id')
    
    if not video_id:
        return jsonify({"status": "error", "message": "ID inválido."}), 400

    youtube = get_youtube_service(user['uid'])
    if not youtube: return jsonify({"status": "error", "message": "Canal YouTube não conectado."}), 400

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
    user = get_user_from_token()
    if not user: return jsonify({"status": "error", "message": "Não autenticado."}), 401
    
    query = request.args.get('q')
    if not query:
        return jsonify({"status": "success", "channels": []})

    youtube = get_youtube_service(user['uid'])
    if not youtube: return jsonify({"status": "error", "message": "Canal YouTube não conectado."}), 400

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
    user = get_user_from_token()
    if not user: return jsonify({"status": "error", "message": "Não autenticado."}), 401

    youtube = get_youtube_service(user['uid'])
    if not youtube: return jsonify({"status": "error", "message": "Canal YouTube não conectado."}), 400
    
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
    return render_template('index.html')

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/app')
def dashboard():
    return render_template('home.html')

@app.route('/plans')
def plans_page():
    return render_template('plans.html')

@app.route('/profile')
def profile():
    return render_template('profile.html')

@app.route('/tips')
def tips():
    return render_template('tips.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, '..'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/apple-touch-icon.png')
def apple_touch_icon():
    return send_from_directory(os.path.join(app.root_path, '..'), 'apple-touch-icon.png', mimetype='image/png')

# --- CRIAÇÃO DE CHECKOUT (INTEGRAÇÃO DIRETA) ---
@app.route('/create_checkout', methods=['POST'])
def create_checkout():
    user = get_user_from_token()
    if not user: return jsonify({"status": "error", "message": "Não autenticado"}), 401
    
    # Pegue sua API Key no painel do Abacate Pay -> Desenvolvedor
    api_key = os.environ.get("ABACATE_API_KEY")
    if not api_key:
        return jsonify({"status": "error", "message": "Configuração de pagamento incompleta no servidor"}), 500

    # URL da API do Abacate Pay (Verifique a documentação oficial para o endpoint exato de 'create checkout')
    # Supondo estrutura padrão de APIs de pagamento:
    api_url = "https://api.abacatepay.com/v1/checkout/sessions" 

    payload = {
        "products": ["prod_XWJeWTKMQpy52SCGPMQdgE23"], # Seu ID de produto
        "customer": {
            "email": user['email']
        },
        "metadata": {
            "userId": user['uid'] # O pulo do gato: enviamos o ID para receber de volta no webhook
        },
        "successUrl": request.host_url + "app",
        "cancelUrl": request.host_url + "plans"
    }
    
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        response = requests.post(api_url, json=payload, headers=headers)
        data = response.json()
        
        # Retorna a URL de pagamento gerada
        return jsonify({"status": "success", "url": data.get("url")})
    except Exception as e:
        logger.error(f"Erro ao criar checkout: {e}")
        return jsonify({"status": "error", "message": "Erro ao processar pagamento"}), 500

# --- WEBHOOK ABACATE PAY ---
@app.route('/webhook/abacate', methods=['POST'])
def abacate_webhook():
    data = request.get_json()
    logger.info(f"Webhook Abacate recebido: {data}")
    
    # Tenta pegar o UID do metadata (Muito mais seguro que email)
    user_uid = data.get('metadata', {}).get('userId')
    customer_email = data.get('customer', {}).get('email')
    status = data.get('status') # ex: 'paid', 'completed'
    
    # Verifica se o status indica pagamento aprovado
    if status in ['paid', 'completed']:
        try:
            if user_uid:
                # Busca direto pelo ID (Ideal)
                user_ref = db.collection('users').document(user_uid)
                uid_log = user_uid
            elif customer_email:
                # Fallback: Busca pelo email se não tiver metadata
                user = auth.get_user_by_email(customer_email)
                user_ref = db.collection('users').document(user.uid)
                uid_log = user.uid
            else:
                return jsonify({"status": "ignored", "reason": "no_user_data"}), 200
            
            # Atualiza plano no Firestore
            # Define plano PRO e dá créditos ilimitados (ou um número alto)
            user_ref.set({
                'plan': 'pro',
                'credits': 999999,
                'updated_at': datetime.datetime.now()
            }, merge=True)
            
            logger.info(f"Plano PRO ativado para: {uid_log}")
            
            return jsonify({"status": "success"}), 200
        except firebase_admin.auth.UserNotFoundError:
            logger.warning(f"Usuário não encontrado para o email: {customer_email}")
            return jsonify({"status": "ignored", "reason": "user_not_found"}), 200
        except Exception as e:
            logger.error(f"Erro webhook: {e}")
            return jsonify({"status": "error"}), 500
            
    return jsonify({"status": "ignored"}), 200

@app.route('/send', methods=['POST'])
def send_message():
    user = get_user_from_token()
    if not user: return jsonify({"status": "error", "message": "Não autenticado."}), 401
    uid = user['uid']

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

    # --- VERIFICAÇÃO DE PLANO/CRÉDITOS ---
    user_ref = db.collection('users').document(uid)
    user_doc = user_ref.get()
    user_data = user_doc.to_dict() if user_doc.exists else {}
    
    today_str = datetime.date.today().isoformat()
    plan = user_data.get('plan', 'free')
    credits = user_data.get('credits', 10) # Plano Free começa com 10 créditos
    
    # --- LIMITE DIÁRIO (PROTEÇÃO DE COTA) ---
    last_usage_date = user_data.get('last_usage_date')
    daily_count = user_data.get('daily_count', 0)

    # Reseta contador se mudou o dia
    if last_usage_date != today_str:
        daily_count = 0
        user_ref.update({'daily_count': 0, 'last_usage_date': today_str})

    # Definição de Limites Diários
    # Free: limitado pelos créditos totais, mas também colocamos um teto diário
    # Pro: "Ilimitado" mas com teto técnico para não estourar a API do Google (ex: 200 envios/dia)
    DAILY_LIMIT = 200 if plan == 'pro' else 10

    if daily_count >= DAILY_LIMIT:
         return jsonify({
            "status": "error", 
            "message": f"Limite diário de segurança atingido ({DAILY_LIMIT} envios). Tente novamente amanhã."
        }), 429

    if plan == 'free' and credits <= 0:
        return jsonify({
            "status": "error", 
            "message": "Limite gratuito atingido. Faça upgrade para continuar!"
        }), 402 # Payment Required

    # --- CONECTA AO YOUTUBE ---
    youtube = get_youtube_service(uid)
    if not youtube:
        return jsonify({"status": "error", "message": "Canal não conectado."}), 400

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
            
            # Garante que o campo de histórico existe
            if 'usage_history' not in user_data:
                user_ref.set({'usage_history': {}}, merge=True)

            # Atualiza contadores
            updates = {'daily_count': firestore.Increment(1), f'usage_history.{today_str}': firestore.Increment(1)}
            if plan == 'free':
                updates['credits'] = firestore.Increment(-1)
            
            user_ref.update(updates)
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
            
            # Garante que o campo de histórico existe
            if 'usage_history' not in user_data:
                user_ref.set({'usage_history': {}}, merge=True)

            # Atualiza contadores
            updates = {'daily_count': firestore.Increment(1), f'usage_history.{today_str}': firestore.Increment(1)}
            if plan == 'free':
                updates['credits'] = firestore.Increment(-1)
            
            user_ref.update(updates)
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