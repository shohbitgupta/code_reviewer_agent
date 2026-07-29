MAX_RETRY_COUNT = 3


def should_retry(attempt_count):
    return attempt_count < MAX_RETRY_COUNT
