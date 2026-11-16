from app.copilot_errors import error_headers


def test_error_headers_falls_back_to_exception_level_hint():
    """A reconstructed response with unrelated headers must not hide the
    Retry-After hint that _invoke captured at the exception level (review
    finding on PR #106, head be8f4ab)."""
    class Response:
        headers = {"Content-Type": "application/json"}

    class StatusError(Exception):
        status_code = 429
        response = Response()
        headers = {"Retry-After": "7"}

    assert error_headers(StatusError("rate limited")) == {"Retry-After": "7"}

    class Hintless(Exception):
        status_code = 429
        response = Response()

    assert error_headers(Hintless("rate limited")) == {}

    class InvalidHint(Exception):
        status_code = 503
        response = Response()
        headers = {"Retry-After": "not-a-number"}

    assert error_headers(InvalidHint("unavailable")) == {}
