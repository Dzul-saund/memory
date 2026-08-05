import re
from eth_account import Account
from py_clob_client_v2 import ClobClient

pk = re.search(r"^PRIVATE_KEY=(.+)", open("bot4.env").read(), re.M).group(1).strip()
print("=== SIGNER (address from your PRIVATE_KEY) ===")
print(Account.from_key(pk).address)

print("=== Deriving API keys with this key ===")
c = ClobClient("https://clob.polymarket.com", chain_id=137, key=pk)
creds = c.create_or_derive_api_key()
print("RAW:", creds)
for a in ("api_key", "api_secret", "api_passphrase"):
    print(a, "=", getattr(creds, a, "(n/a)"))
