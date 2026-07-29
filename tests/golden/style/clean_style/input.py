from os.path import join, exists


def connect_with_retry(host):
    try:
        return connect(host)
    except ConnectionError as exc:
        logger.warning("Connection to %s failed: %s", host, exc)
        return None
