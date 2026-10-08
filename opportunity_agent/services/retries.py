import logging
import time

import requests


def request_with_exponential_backoff(operation, *, description, attempts=3):
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise ValueError('attempts must be a positive integer.')
    logger = logging.getLogger(__name__)
    for attempt in range(attempts):
        try:
            return operation()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            retryable = status is None or status == 429 or status >= 500
            if not retryable or attempt + 1 == attempts:
                raise
            delay = 2 ** attempt
            logger.warning(
                'Transient request failure during %s; retry %s/%s in %s seconds.',
                description,
                attempt + 1,
                attempts - 1,
                delay,
                exc_info=True,
            )
            time.sleep(delay)
