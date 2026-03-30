import logging
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

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
            # Isso não é necessariamente um erro, pode ser um vídeo normal.
            # logger.info(f"Vídeo {video_id} não é uma transmissão ao vivo ou não tem detalhes disponíveis.")
            return None

        live_chat_id = live_details.get("activeLiveChatId")
        if not live_chat_id:
            # Também não é um erro, a live pode não ter chat ou ter acabado.
            # logger.info(f"Chat ao vivo não está ativo para o vídeo {video_id}.")
            return None

        return live_chat_id

    except HttpError as e:
        logger.error(f"Erro ao buscar liveChatId para o vídeo {video_id}: {e}")
        return None
