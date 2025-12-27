# portfolio/app.py
from __future__ import annotations
import os
from flask import Flask, jsonify
from common.utils import load_env
from common import logging as log

from routes.home import bp as home_bp

SERVICE   = os.getenv("REFLEX__SERVICE", "portfolio.api")
COMPONENT = __name__

app = Flask(SERVICE)
app.register_blueprint(home_bp)

@app.get("/healthz")
def healthz():
    return jsonify({"service": SERVICE, "status": "ok"})

def main():
    load_env()
    # Prefer explicit PORTFOLIO_PORT, then FLASK_RUN_PORT, then default 7085
    port = int(os.getenv("PORTFOLIO_PORT") or os.getenv("FLASK_RUN_PORT") or "7085")
    log.info(COMPONENT, "startup", service=SERVICE, host="127.0.0.1", port=port)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
