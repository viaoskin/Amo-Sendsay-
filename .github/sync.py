import os
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===== НАСТРОЙКИ =====
AMO_DOMAIN    = os.environ["AMO_DOMAIN"]        # например "brainboxvc.amocrm.ru"
AMO_TOKEN     = os.environ["AMO_TOKEN"]
SENDSAY_ACC   = os.environ["SENDSAY_ACC"]
SENDSAY_KEY   = os.environ["SENDSAY_KEY"]
SENDSAY_FIELD = "custom.q3"                     # КОД поля в Sendsay

PIPELINE_ID = 6125238
STATUS_IDS  = [53006038, 64373018]              # "Зарегистрирован" и "Зарегистрирован. В работе"
WINDOW_SEC  = 24 * 60 * 60                      # окно поиска: сутки
# =====================

BASE = f"https://{AMO_DOMAIN}/api/v4"
SENDSAY_URL = f"https://api.sendsay.ru/general/api/v100/json/{SENDSAY_ACC}"

retry = Retry(
    total=5, connect=5, read=5, backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
amo = requests.Session()
amo.headers["Authorization"] = f"Bearer {AMO_TOKEN}"
amo.mount("https://", HTTPAdapter(max_retries=retry))


def mask(email):
    name, _, domain = email.partition("@")
    return f"{name[:2]}***@{domain}"


def with_retries(func, attempts=4):
    for i in range(attempts):
        try:
            return func()
        except (requests.ConnectionError, requests.Timeout):
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))


def amo_get(path, params=None):
    def call():
        r = amo.get(f"{BASE}/{path}", params=params, timeout=30)
        if r.status_code == 204:
            return {}
        r.raise_for_status()
        return r.json()
    data = with_retries(call)
    time.sleep(0.3)                             # лимит amo ~7 запросов/сек
    return data


def get_users():
    data = amo_get("users", {"limit": 250})
    return {u["id"]: u["name"] for u in data.get("_embedded", {}).get("users", [])}


def get_leads():
    since = int(time.time()) - WINDOW_SEC
    page, leads = 1, []
    while True:
        params = {
            "with": "contacts",
            "limit": 250,
            "page": page,
            "filter[updated_at][from]": since,
        }
        for i, status_id in enumerate(STATUS_IDS):
            params[f"filter[statuses][{i}][pipeline_id]"] = PIPELINE_ID
            params[f"filter[statuses][{i}][status_id]"] = status_id
        data = amo_get("leads", params)
        batch = data.get("_embedded", {}).get("leads", [])
        leads += batch
        if len(batch) < 250:
            return leads
        page += 1


def get_email(lead):
    contacts = lead.get("_embedded", {}).get("contacts", [])
    contacts.sort(key=lambda c: not c.get("is_main"))   # основной контакт первым
    for c in contacts:
        data = amo_get(f"contacts/{c['id']}")
        for f in data.get("custom_fields_values") or []:
            if f.get("field_code") == "EMAIL" and f["values"]:
                return f["values"][0]["value"].strip().lower()
    return None


def sendsay_call(body):
    def call():
        r = requests.post(SENDSAY_URL, json={"apikey": SENDSAY_KEY, **body}, timeout=30)
        r.raise_for_status()
        return r.json()
    data = with_retries(call)
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data


def sendsay_state(email):
    """Возвращает 'no_contact', 'has_value' или 'empty'."""
    data = sendsay_call({
        "action": "stat.uni",
        "select": ["member.email", f"member.datakey.{SENDSAY_FIELD}"],
        "filter": [{"a": "member.email", "op": "==", "v": email}],
    })
    rows = data.get("list") or []
    if not rows:
        return "no_contact"
    for row in rows:
        for cell in row[1:]:
            if cell not in (None, "", []):
                return "has_value"
    return "empty"


def sendsay_set_manager(email, manager):
    sendsay_call({
        "action": "member.set",
        "email": email,
        "datakey": [[SENDSAY_FIELD, "set", manager]],
    })


def main():
    users = get_users()
    leads = get_leads()
    print(f"Найдено сделок: {len(leads)}")
    stats = {}
    for lead in leads:
        try:
            manager = users.get(lead.get("responsible_user_id"))
            email = get_email(lead)
            if not (manager and email):
                print(f"skip lead {lead['id']}: manager={bool(manager)} email={bool(email)}")
                stats["skip"] = stats.get("skip", 0) + 1
                continue
            state = sendsay_state(email)
            if state == "no_contact":
                print(f"wait lead {lead['id']}: {mask(email)} ещё нет в Sendsay")
                stats["wait"] = stats.get("wait", 0) + 1
            elif state == "has_value":
                stats["keep"] = stats.get("keep", 0) + 1
            else:
                sendsay_set_manager(email, manager)
                print(f"ok lead {lead['id']} -> {mask(email)}: {manager}")
                stats["ok"] = stats.get("ok", 0) + 1
        except Exception as e:
            print(f"ERR lead {lead['id']}: {e}")
            stats["err"] = stats.get("err", 0) + 1
    print("Итого:", stats)


if __name__ == "__main__":
    main()
