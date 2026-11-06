import json
import logging
import sys

from app.errors import PermanentIngestionError
from app.services.pdf_extract import extract_pages


def main() -> None:
    logging.disable(logging.CRITICAL)
    if len(sys.argv) != 3:
        sys.stdout.write(json.dumps({"error_code": "unexpected"}))
        raise SystemExit(1)
    try:
        pages = extract_pages(sys.argv[1], int(sys.argv[2]))
    except PermanentIngestionError as exc:
        sys.stdout.write(json.dumps({"error_code": exc.code}))
        raise SystemExit(2) from None
    except Exception:
        sys.stdout.write(json.dumps({"error_code": "corrupt"}))
        raise SystemExit(2) from None
    sys.stdout.write(json.dumps({"pages": pages}))


if __name__ == "__main__":
    main()
