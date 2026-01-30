import os
import time
import random
import logging
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Configuração de Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Escopos necessários para postar comentários
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"
CLIENT_SECRETS_FILE = "client_secret.json"
TOKEN_FILE = "token.json"

def get_authenticated_service():
    """
    Realiza a autenticação OAuth 2.0 e retorna o objeto de serviço da API.
    Gerencia o salvamento e refresh do token automaticamente.
    """
    creds = None
    
    # O arquivo token.json armazena os tokens de acesso e atualização do usuário
    # e é criado automaticamente quando o fluxo de autorização é concluído pela primeira vez.
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    
    # Se não houver credenciais válidas disponíveis, deixe o usuário fazer login.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("Token atualizado com sucesso.")
            except Exception as e:
                logger.error(f"Erro ao atualizar token: {e}")
                os.remove(TOKEN_FILE)
                return get_authenticated_service()
        else:
            if not os.path.exists(CLIENT_SECRETS_FILE):
                logger.critical(f"Arquivo '{CLIENT_SECRETS_FILE}' não encontrado.")
                return None

            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRETS_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)
        
        # Salva as credenciais para a próxima execução
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
            logger.info("Novo token salvo.")

    try:
        return build(API_SERVICE_NAME, API_VERSION, credentials=creds)
    except Exception as e:
        logger.error(f"Falha ao construir o serviço da API: {e}")
        return None

def post_comment(youtube, video_id, text):
    """
    Publica um comentário de nível superior em um vídeo específico.
    
    Args:
        youtube: O objeto de serviço autenticado.
        video_id (str): O ID do vídeo (ex: 'dQw4w9WgXcQ').
        text (str): O conteúdo do comentário.
    """
    if not youtube:
        logger.error("Serviço da API não inicializado.")
        return

    # Rate Limiting: Pausa aleatória entre 2 e 5 segundos para comportamento humano
    sleep_time = random.uniform(2.0, 5.0)
    logger.info(f"Aguardando {sleep_time:.2f}s para respeitar rate limits...")
    time.sleep(sleep_time)

    try:
        request = youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {
                        "snippet": {
                            "textOriginal": text
                        }
                    }
                }
            }
        )
        response = request.execute()
        logger.info(f"Comentário postado com sucesso! ID: {response['id']}")
        return response

    except HttpError as e:
        # Análise do erro retornado pela API
        error_content = json.loads(e.content)
        error_reason = error_content.get('error', {}).get('errors', [{}])[0].get('reason', '')
        
        if error_reason == 'quotaExceeded':
            logger.critical("ERRO CRÍTICO: Cota da API excedida para hoje.")
            # Aqui você poderia implementar uma lógica para parar o script por 24h
        elif error_reason == 'commentsDisabled':
            logger.warning(f"Comentários estão desativados para o vídeo {video_id}.")
        elif e.resp.status == 404:
            logger.warning(f"Vídeo {video_id} não encontrado.")
        else:
            logger.error(f"Erro desconhecido na API: {e}")
            
    except Exception as e:
        logger.error(f"Erro inesperado: {e}")

def get_live_chat_id(youtube, video_id):
    """
    Obtém o ID do chat ao vivo (liveChatId) a partir do ID do vídeo.
    Necessário para postar mensagens em lives.
    """
    try:
        response = youtube.videos().list(
            part="liveStreamingDetails",
            id=video_id
        ).execute()

        items = response.get("items", [])
        if not items:
            logger.error(f"Vídeo {video_id} não encontrado.")
            return None

        live_details = items[0].get("liveStreamingDetails")
        if not live_details:
            logger.error(f"Vídeo {video_id} não é uma transmissão ao vivo ou não tem detalhes disponíveis.")
            return None

        live_chat_id = live_details.get("activeLiveChatId")
        if not live_chat_id:
            logger.error(f"Chat ao vivo não está ativo para o vídeo {video_id}.")
            return None

        return live_chat_id

    except HttpError as e:
        logger.error(f"Erro ao buscar liveChatId: {e}")
        return None

def post_live_chat_message(youtube, live_chat_id, text):
    """
    Envia uma mensagem para o chat ao vivo.
    """
    try:
        youtube.liveChatMessages().insert(
            part="snippet",
            body={
                "snippet": {
                    "liveChatId": live_chat_id,
                    "type": "textMessageEvent",
                    "textMessageDetails": {
                        "messageText": text
                    }
                }
            }
        ).execute()
        logger.info(f"Mensagem enviada no chat: {text}")
    except HttpError as e:
        logger.error(f"Erro ao enviar mensagem no chat: {e}")

# --- Exemplo de Uso ---
if __name__ == "__main__":
    # 1. Autenticar
    service = get_authenticated_service()

    if service:
        # 2. Definir dados (Em um caso real, isso viria de uma lista ou banco de dados)
        video_alvo = "ID_DO_VIDEO_AQUI" # Substitua pelo ID real do vídeo (ex: dQw4w9WgXcQ)
        
        if video_alvo == "ID_DO_VIDEO_AQUI":
            video_alvo = input("Por favor, insira o ID do vídeo/live do YouTube: ").strip()
        
        # EXEMPLO 1: Comentário em vídeo normal
        # mensagem = "Ótimo conteúdo! Obrigado por compartilhar."
        # post_comment(service, video_alvo, mensagem)

        # EXEMPLO 2: Mensagem em Chat ao Vivo (Live)
        # Para lives, primeiro precisamos do ID do chat
        live_chat_id = get_live_chat_id(service, video_alvo)
        if live_chat_id:
             post_live_chat_message(service, live_chat_id, "Olá chat! Testando bot.")
        else:
             logger.warning("Não foi possível obter o ID do chat. Verifique se a live está ativa.")
