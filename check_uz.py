"""One live read-only check; requires UZ env vars, no Telegram token."""
import json
from provider import UZ,ProviderError
try:print(json.dumps(UZ().check(),ensure_ascii=False,indent=2))
except ProviderError as e:print(str(e));raise SystemExit(2)
except Exception:print('Неизвестный формат ответа УЗ. Требуется проверка адаптера.');raise SystemExit(3)
