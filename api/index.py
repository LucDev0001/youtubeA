import os
import json
import logging
import datetime
import random
import requests
import re
from flask import Flask, request, render_template, jsonify, session, redirect, url_for, send_from_directory
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError
from werkzeug.middleware.proxy_fix import ProxyFix
import firebase_admin
from firebase_admin import credentials, firestore, auth
import abacatepay

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

ADMIN_UID = "BDZUzrq5kSSG5TMO3s2LI15gWEu2"

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
        include_granted_scopes='true',
        prompt='consent select_account'
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
        
        # Obtém informações do canal conectado para salvar no banco
        try:
            youtube_service = build("youtube", "v3", credentials=flow.credentials, cache_discovery=False)
            channels_response = youtube_service.channels().list(part="snippet", mine=True).execute()
            channel_info = {}
            if channels_response.get("items"):
                snippet = channels_response["items"][0]["snippet"]
                channel_info = {
                    "title": snippet["title"],
                    "thumbnail": snippet["thumbnails"]["default"]["url"]
                }
        except Exception as e:
            logger.error(f"Erro ao obter detalhes do canal: {e}")
            channel_info = {}

        creds_dict = credentials_to_dict(flow.credentials)
        
        # SALVA NO FIRESTORE
        if user_uid:
            db.collection('users').document(user_uid).set({
                'youtube_credentials': creds_dict,
                'youtube_connected': True,
                'youtube_channel': channel_info
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

    try:
        # --- MODO SCRAPING (Economia de Cota: 1 unidade por chamada) ---
        # Busca o HTML da página do vídeo para extrair metadados sem gastar cota
        url = f"https://www.youtube.com/watch?v={video_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        resp = requests.get(url, headers=headers)
        html = resp.text

        # Extração via Regex (Mais leve que carregar bibliotecas pesadas)
        title_match = re.search(r'<meta name="title" content="(.*?)">', html)
        title = title_match.group(1) if title_match else "Título desconhecido"

        # Tenta pegar o nome do canal (autor)
        author_match = re.search(r'<link itemprop="name" content="(.*?)">', html)
        channel_title = author_match.group(1) if author_match else "Canal desconhecido"

        # Thumbnail
        thumb_match = re.search(r'<meta property="og:image" content="(.*?)">', html)
        thumbnail = thumb_match.group(1) if thumb_match else ""

        # Verifica se é live (procura por indicadores de transmissão no HTML ou JSON embutido)
        is_live = '"isLive":true' in html or "liveStreamability" in html
        
        return jsonify({
            "status": "success",
            "title": title,
            "channel": channel_title,
            "thumbnail": thumbnail,
            "is_live": is_live
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

    try:
        # --- MODO SCRAPING (Economia de Cota: 100 unidades por chamada!) ---
        # sp=EgIQAg%253D%253D força o filtro para "Canais"
        search_url = f"https://www.youtube.com/results?search_query={query}&sp=EgIQAg%253D%253D"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
        }
        
        resp = requests.get(search_url, headers=headers)
        html = resp.text
        
        channels = []
        # Regex simplificado para encontrar dados do canal no JSON embutido (ytInitialData)
        # Nota: Em produção, usar uma lib como 'youtube-search-python' é mais robusto, 
        # mas este regex resolve para um MVP sem dependências extras.
        
        # Procura por padrões de channelId e title próximos
        # Esta é uma aproximação. O YouTube muda o HTML frequentemente.
        ids = re.findall(r'\"channelId\":\"(UC[\w-]{22})\"', html)
        titles = re.findall(r'\"title\":{\"simpleText\":\"(.*?)\"}', html)
        
        # Pega os primeiros 5 únicos
        seen = set()
        for i, cid in enumerate(ids):
            if cid not in seen and i < len(titles) and len(channels) < 5:
                channels.append({
                    "id": cid,
                    "title": titles[i],
                    "thumbnail": "https://www.gstatic.com/youtube/img/channels/avatar_default_std.png" # Simplificação
                })
                seen.add(cid)

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
    if not page_token:
        page_token = None
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

    except HttpError as e:
        logger.error(f"YouTube API Error: {e}")
        if e.resp.status in [403, 429]:
             return jsonify({"status": "error", "message": "Cota da API excedida. Tente mais tarde."}), 429
        return jsonify({"status": "error", "message": f"Erro na API do YouTube: {e}"}), 500
    except RefreshError:
        return jsonify({"status": "error", "message": "Sessão do YouTube expirada. Reconecte o canal."}), 401
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
    # Busca Preço Atual do Firestore para exibir no frontend
    settings_ref = db.collection('settings').document('general')
    settings_doc = settings_ref.get()
    price = 2990
    if settings_doc.exists:
        price = settings_doc.to_dict().get('pro_price', 2990)
    return render_template('plans.html', price=price)

@app.route('/profile')
def profile():
    return render_template('profile.html')

@app.route('/tips')
def tips():
    return render_template('tips.html')

@app.route('/thank-you')
def thank_you_page():
    return render_template('thankyou.html')

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

# --- ROTAS ADMIN ---
@app.route('/admin')
def admin_page():
    return render_template('admin.html')

@app.route('/api/admin/data', methods=['GET'])
def admin_get_data():
    user_token = get_user_from_token()
    if not user_token or user_token['uid'] != ADMIN_UID:
        return jsonify({"status": "error", "message": "Acesso negado"}), 403

    # Busca Preço Atual
    settings_ref = db.collection('settings').document('general')
    settings_doc = settings_ref.get()
    price = 2990
    if settings_doc.exists:
        price = settings_doc.to_dict().get('pro_price', 2990)

    # Busca Usuários
    users_ref = db.collection('users')
    docs = users_ref.stream()
    users_list = []
    for doc in docs:
        u = doc.to_dict()
        created_at = u.get('created_at')
        if created_at:
            # Converte timestamp do Firestore para string
            if hasattr(created_at, 'isoformat'):
                created_at = created_at.isoformat()
            else:
                created_at = str(created_at)
        
        users_list.append({
            'uid': doc.id,
            'email': u.get('email', 'N/A'),
            'plan': u.get('plan', 'free'),
            'credits': u.get('credits', 0),
            'daily_count': u.get('daily_count', 0),
            'created_at': created_at
        })

    return jsonify({
        "status": "success",
        "price": price / 100, # Envia como float (ex: 29.90)
        "users": users_list
    })

@app.route('/api/admin/price', methods=['POST'])
def admin_update_price():
    user_token = get_user_from_token()
    if not user_token or user_token['uid'] != ADMIN_UID:
        return jsonify({"status": "error", "message": "Acesso negado"}), 403
    
    data = request.get_json()
    new_price = data.get('price')
    
    if new_price is None:
        return jsonify({"status": "error", "message": "Preço inválido"}), 400
        
    # Converte para centavos
    price_cents = int(float(new_price) * 100)
    
    db.collection('settings').document('general').set({
        'pro_price': price_cents
    }, merge=True)
    
    return jsonify({"status": "success", "message": "Preço atualizado"})

# --- CRIAÇÃO DE CHECKOUT (INTEGRAÇÃO DIRETA) ---
@app.route('/create_checkout', methods=['POST'])
def create_checkout():
    user_token = get_user_from_token()
    if not user_token: return jsonify({"status": "error", "message": "Não autenticado"}), 401
    
    # Pegue sua API Key no painel do Abacate Pay -> Desenvolvedor
    api_key = os.environ.get("ABACATE_API_KEY")
    if not api_key:
        return jsonify({"status": "error", "message": "Configuração de pagamento incompleta no servidor"}), 500

    try:
        # Busca dados completos do usuário no Firestore
        uid = user_token['uid']
        user_doc = db.collection('users').document(uid).get()
        user_data = user_doc.to_dict() if user_doc.exists else {}

        # Usa dados reais ou fallback se não existirem (para usuários antigos)
        name = user_data.get('name') or user_token.get('name') or "Cliente"
        email = user_data.get('email') or user_token.get('email')
        phone = user_data.get('phone') or "11999999999"
        cpf = user_data.get('cpf') or "12345678909"

        # Busca preço dinâmico do banco de dados
        settings_ref = db.collection('settings').document('general')
        settings_doc = settings_ref.get()
        price = 2990 # Valor padrão
        if settings_doc.exists:
            price = settings_doc.to_dict().get('pro_price', 2990)

        client = abacatepay.AbacatePay(api_key)

        # 1. Criar o cliente primeiro para obter o ID
        customer = client.customers.create(
            name=name,
            email=email,
            cellphone=phone,
            taxId=cpf,
            metadata={"userId": uid}
        )
        
        logger.info(f"Customer criado: {customer}")
        
        # Garante que pegamos o ID corretamente (objeto ou dict)
        customer_id = None
        if isinstance(customer, dict):
            customer_id = customer.get('id') or customer.get('data', {}).get('id')
        else:
            customer_id = getattr(customer, 'id', None)
            if not customer_id and hasattr(customer, 'data'):
                data_obj = customer.data
                customer_id = data_obj.get('id') if isinstance(data_obj, dict) else getattr(data_obj, 'id', None)

        if not customer_id:
            raise ValueError(f"ID do cliente não encontrado. Resposta: {customer}")

        billing = client.billing.create(
            frequency="ONE_TIME",
            methods=["PIX"],
            products=[
                {
                    "external_id": "plan-pro",
                    "name": "Plano PRO - YouTube Growth Bot",
                    "description": "Acesso ilimitado ao bot e recursos premium",
                    "quantity": 1,
                    "price": price # Usa o preço do banco de dados
                }
            ],
            return_url=request.host_url + "thank-you",
            completion_url=request.host_url + "thank-you",
            customer_id=customer_id
        )
        
        logger.info(f"Billing criado: {billing}")
        
        return jsonify({"status": "success", "url": billing.url})
    except Exception as e:
        error_msg = str(e)
        # Tenta extrair detalhes do erro da SDK se disponível para debug
        if hasattr(e, 'response'):
             try:
                 error_msg += f" | Detalhes: {e.response.text}"
             except:
                 pass

        logger.error(f"Erro ao criar checkout: {error_msg}")
        return jsonify({"status": "error", "message": f"Erro no servidor: {error_msg}"}), 500

# --- WEBHOOK ABACATE PAY ---
@app.route('/webhook/abacate', methods=['POST'])
def abacate_webhook():
    # 1. Implemente Segurança (Autenticação Simples)
    webhook_secret = request.args.get('webhookSecret')
    expected_secret = os.environ.get('ABACATE_WEBHOOK_SECRET')
    
    if not expected_secret:
        logger.error("ERRO CRÍTICO: A variável de ambiente ABACATE_WEBHOOK_SECRET não está definida no servidor.")
        return jsonify({"error": "Server Configuration Error"}), 500

    if webhook_secret != expected_secret:
        logger.warning(f"Acesso negado ao webhook. Secret recebido: '{webhook_secret}'")
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    logger.info(f"Webhook Abacate recebido: {data}")
    # Log extra para debug da estrutura
    logger.info(f"Webhook Data Dump: {json.dumps(data)}")
    
    # 2. Corrija o Parsing do JSON (Estrutura Oficial)
    event = data.get('event')
    
    if event == "billing.paid":
        try:
            # Navegação segura no JSON (suporta variações da estrutura)
            payload_data = data.get('data', {})
            billing = payload_data.get('billing') or payload_data
            
            if not billing:
                logger.error("Objeto 'billing' não encontrado no payload.")
                return jsonify({"status": "ignored", "reason": "no_billing"}), 200

            customer = billing.get('customer')
            user_uid = None
            customer_email = None

            # CENÁRIO A: Customer é um objeto completo (Dicionário)
            if isinstance(customer, dict):
                metadata = customer.get('metadata', {})
                # Tenta pegar userId do metadata, se existir
                user_uid = metadata.get('userId') 
                
                # O email pode estar na raiz do customer ou dentro do metadata (conforme log recebido)
                customer_email = customer.get('email') or metadata.get('email')
            
            # CENÁRIO B: Customer é apenas um ID (String) - BUSCA NA API
            elif isinstance(customer, str):
                logger.info(f"Customer ID recebido ({customer}). Buscando detalhes na API...")
                api_key = os.environ.get("ABACATE_API_KEY")
                if api_key:
                    headers = {"Authorization": f"Bearer {api_key}"}
                    # Busca dados do cliente para recuperar o userId do metadata
                    resp = requests.get(f"https://api.abacatepay.com/v1/customers/{customer}", headers=headers)
                    if resp.ok:
                        cust_data = resp.json().get('data', {})
                        user_uid = cust_data.get('metadata', {}).get('userId')
                        customer_email = cust_data.get('email')
                    else:
                        logger.error(f"Falha ao buscar customer na API: {resp.text}")
            
            user_ref = None
            uid_log = None

            logger.info(f"Processando Webhook para UID: {user_uid} | Email: {customer_email}")

            # 3. Lógica de Atualização: Prioridade ID > Email
            if user_uid:
                user_ref = db.collection('users').document(user_uid)
                uid_log = user_uid
            elif customer_email:
                try:
                    user = auth.get_user_by_email(customer_email)
                    user_ref = db.collection('users').document(user.uid)
                    uid_log = user.uid
                except firebase_admin.auth.UserNotFoundError:
                    logger.warning(f"Usuário não encontrado para o email: {customer_email}")
                    return jsonify({"status": "ignored", "reason": "user_not_found"}), 200
            
            if user_ref:
                user_ref.set({
                    'plan': 'pro',
                    'credits': 999999,
                    'updated_at': datetime.datetime.now()
                }, merge=True)
                
                logger.info(f"Plano PRO ativado para: {uid_log}")
                return jsonify({"status": "success"}), 200
            else:
                logger.warning("Webhook recebido sem userId ou email válido.")
                return jsonify({"status": "ignored", "reason": "no_user_data"}), 200

        except Exception as e:
            logger.error(f"Erro ao processar webhook: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
            
    # 4. Tratamento de Erros e Resposta (Eventos ignorados)
    return jsonify({"status": "ignored", "reason": f"event_{event}_not_handled"}), 200

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