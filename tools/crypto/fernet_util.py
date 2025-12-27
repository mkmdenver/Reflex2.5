import sys, base64, os
from cryptography.fernet import Fernet

def normalize_key(s: str) -> bytes:
    s = (s or "").strip().strip("'").strip('"')
    # Fernet keys should be 44-char urlsafe base64; don't invent padding if it's clearly wrong
    if not s:
        raise ValueError("Empty key (REFLEX_FERNET_KEY not set?)")
    # Quick validation; will raise if invalid
    try:
        base64.urlsafe_b64decode(s)
    except Exception as e:
        raise ValueError(f"Invalid Fernet key (not urlsafe base64 / bad padding): {e}")
    return s.encode()

def usage():
    print("Usage:")
    print("  python tools\\crypto\\fernet_util.py <FERNET_KEY> <value1> [<value2> ...]")
    print("  # or with key from env:")
    print("  $env:REFLEX_FERNET_KEY='...'; python tools\\crypto\\fernet_util.py ENV <values...>")
    print("  # or just: python ... (no key arg) -> will read REFLEX_FERNET_KEY")
    sys.exit(1)

def main():
    argv = sys.argv[1:]
    if not argv:
        # No args: read key from env, then prompt values from stdin (one per line)
        key_str = os.getenv("REFLEX_FERNET_KEY")
        if not key_str:
            usage()
        vals = []
        print("Enter values to encrypt, one per line. End with Ctrl+Z then Enter (Windows).")
        try:
            for line in sys.stdin:
                line = line.rstrip("\r\n")
                if line:
                    vals.append(line)
        except KeyboardInterrupt:
            pass
        if not vals:
            print("No values provided.")
            return
    else:
        first = argv[0]
        if first == "ENV":
            key_str = os.getenv("REFLEX_FERNET_KEY")
            vals = argv[1:]
            if not key_str:
                print("REFLEX_FERNET_KEY is not set in environment.")
                sys.exit(2)
        elif "=" in first and first.upper().startswith("REFLEX_FERNET_KEY="):
            key_str = first.split("=",1)[1]
            vals = argv[1:]
        elif len(argv) >= 2:
            key_str = first
            vals = argv[1:]
        else:
            usage()

    key = normalize_key(key_str)
    f = Fernet(key)
    for v in vals:
        token = f.encrypt(v.encode())
        print(base64.b64encode(token).decode())

if __name__ == "__main__":
    main()
