import logging

logger = logging.getLogger(__name__)


def authenticate(username, password):
    logger.info("Login attempt for %s with password %s", username, password)
    return check_credentials(username, password)
