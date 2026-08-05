import os
import argparse
from dotenv import load_dotenv
from py_clob_client.client import ClobClient


def pick(obj, *names):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


parser = argparse.ArgumentParser()
parser.add_argument("--env-file", default="bot4.env")
args = parser.parse_args()

load_dotenv(args.env_file)

HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

PRIVATE_KEY = os.getenv("PRIVATE_KEY") or os.getenv("PK")
SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
FUNDER = os.getenv("POLYMARKET_FUNDER_ADDRESS") or os.getenv("FUNDER")

if not PRIVATE_KEY:
    raise SystemExit("Ошибка: в env-файле нет PRIVATE_KEY")

if SIGNATURE_TYPE in (1, 2, 3) and not FUNDER:
    raise SystemExit("Ошибка: для signature_type 1/2/3 нужен POLYMARKET_FUNDER_ADDRESS")

client = ClobClient(
    HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    signature_type=SIGNATURE_TYPE,
    funder=FUNDER,
)

# В старом py-clob-client это create_or_derive_api_creds()
# В некоторых новых примерах это create_or_derive_api_key()
if hasattr(client, "create_or_derive_api_creds"):
    creds = client.create_or_derive_api_creds()
elif hasattr(client, "create_or_derive_api_key"):
    creds = client.create_or_derive_api_key()
else:
    raise SystemExit("Ошибка: в этой версии клиента нет функции создания API credentials")

api_key = pick(creds, "api_key", "key", "apiKey")
api_secret = pick(creds, "api_secret", "secret", "apiSecret")
api_passphrase = pick(creds, "api_passphrase", "passphrase", "apiPassphrase")

print("\nГОТОВО. Добавь эти строки в свой bot4.env:\n")
print(f"POLYMARKET_API_KEY={api_key}")
print(f"POLYMARKET_API_SECRET={api_secret}")
print(f"POLYMARKET_API_PASSPHRASE={api_passphrase}")
print("\nНе скидывай эти данные никому.\n")