def connect_with_retry(host):
    try:
        return connect(host)
    except:
        pass
