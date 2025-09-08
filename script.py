import os
import time
import json
from datetime import datetime
import requests



API_TOKEN = os.environ["JIRA_TOKEN"]
JIRA_URL = os.environ["JIRA_URL"]
TEAMS_WEBHOOK_URL = os.environ["TEAMS_WEBHOOK_URL"]
FILTER_ID = os.environ["FILTER_ID"]

POLL_SECONDS = 60

# Campos para la búsqueda
FIELDS = ["summary", "priority", "updated", "assignee", "reporter", "status", "customfield_10724"]

# Prioridades a vigilar (De momento pongo todas)
JQL = f'filter = {FILTER_ID} AND priority in (Highest, High, Medium, Low)'

# Recomendación de ChatGPT: Endpoint de búsqueda (cambia a /api/3 si tu Jira lo exige)
API_ENDPOINT = f'{JIRA_URL}/rest/api/2/search'
ISSUE_ENDPOINT = f'{JIRA_URL}/rest/api/2/issue'  # /{key}?fields=comment

MAX_RETRIES = 3
TIMEOUT = 15


def jira_get(url, params=None):
    headers = {'Authorization': f'Bearer {API_TOKEN}', 'Accept': 'application/json'}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
            if r.status_code >= 400:
                print(f"[HTTP {r.status_code}] {r.text}")
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                raise
            time.sleep(2 * attempt)


def jira_search(start_at=0, max_results=100):
    params = {
        'jql': JQL,
        'fields': ','.join(FIELDS),
        'startAt': start_at,
        'maxResults': max_results
    }
    return jira_get(API_ENDPOINT, params=params)


def get_last_comment_updated(issue_key: str) -> str:
    """Devuelve el timestamp ISO del último comentario o '' si no hay comentarios."""
    data = jira_get(f"{ISSUE_ENDPOINT}/{issue_key}", params={'fields': 'comment'})
    comments = (((data or {}).get('fields') or {}).get('comment') or {}).get('comments', [])
    if not comments:
        return ''
    # Coge el mas reciente según "updated".
    return max(c.get('updated', '') for c in comments)


def format_issue(issue, reason: str):
    fields = issue.get('fields', {})
    key = issue.get('key', 'N/A')
    summary = fields.get('summary') or 'No summary'
    priority = (fields.get('priority') or {}).get('name') or 'No priority'
    status = (fields.get('status') or {}).get('name') or 'No status'
    assignee = (fields.get('assignee') or {}).get('displayName') or 'Sin asignar'
    reporter = (fields.get('reporter') or {}).get('displayName') or 'No reporter'
    updated = fields.get('updated') or ''
    assignee_group = (fields.get('customfield_10724') or {}).get('name') or 'Sin grupo asignado'

    url = f"{JIRA_URL}/browse/{key}"

    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"[{priority}] {key} — {summary}",
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": "Attention" if priority in ("Highest", "High") else "Default",
                            "wrap": True
                        },
                        {
                            "type": "TextBlock",
                            "text": f"Motivo: {reason}",
                            "wrap": True,
                            "spacing": "Small"
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Estado", "value": status},
                                {"title": "Asignado", "value": assignee},
                                {"title": "Grupo", "value": assignee_group},
                                {"title": "Reporter", "value": reporter},
                                {"title": "Prioridad", "value": priority},
                                {"title": "Updated", "value": updated}
                            ]
                        },
                        {
                            "type": "TextBlock",
                            "text": summary,
                            "wrap": True,
                            "spacing": "Medium"
                        }
                    ],
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "🔗 Abrir en Jira",
                            "url": url
                        }
                    ]
                }
            }
        ]
    }

    # alarma en el pc 
    plain = (
        f"{'-'*50}\n"
        f"[{reason}] {summary}\n"
        f"Priority: {priority}\n"
        f"{url}\n"
        f"{assignee}\n"
        f"{assignee_group}\n"
        f"{'-'*50}\n"
    )

    return card, plain


def send_to_teams(card: dict):
    headers = {'Content-Type': 'application/json'}
    #r = requests.post(TEAMS_WEBHOOK_URL, headers=headers, data=json.dumps(card), timeout=TIMEOUT)
    r = requests.post(TEAMS_WEBHOOK_URL, json=card)
    if r.status_code >= 400:
        print(f"[Teams HTTP {r.status_code}] {r.text}")
    r.raise_for_status()



def main():
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Monitor Jira (Highest/High/Medium/Low) cada {POLL_SECONDS}s")
    # Guarda por issue: { key: {"updated": str, "priority": str, "last_comment": str} }
    seen = {}

    try:
        start_at = 0
        total = 1
        alerts = 0
        
        while start_at < total:
            data = jira_search(start_at=start_at)
            total = data.get('total', 0)
            issues = data.get('issues', [])
            start_at += len(issues)
            
            for issue in issues:
                key = issue.get('key', 'N/A')
                fields = issue.get('fields', {})
                updated = fields.get('updated') or ''
                priority = (fields.get('priority') or {}).get('name') or ''
                prev = seen.get(key)
                
                reason = None
                last_comment_updated = None
                
                if prev is None:
                    # Nuevo issue 
                    # Miramos si tiene comentarios 
                    try:
                        last_comment_updated = get_last_comment_updated(key)
                    except Exception as ce:
                        print(f"[WARN] No se pudo consultar comentarios de {key}: {ce}")
                        last_comment_updated = ''
                        reason = "nuevo"
                else:
                    # ¿Cambió la prioridad?
                    if prev.get('priority') != priority:
                        reason = f"cambio de prioridad ({prev.get('priority')} → {priority})"
                        # ¿Cambió el 'updated'? si es así distinguimos comentario vs otros cambios
                    elif prev.get('updated') != updated:
                        try:
                            last_comment_updated = get_last_comment_updated(key)
                        except Exception as ce:
                            print(f"[WARN] No se pudo consultar comentarios de {key}: {ce}")
                            last_comment_updated = prev.get('last_comment', '')
                            
                        if (last_comment_updated or '') != (prev.get('last_comment') or ''):
                            reason = "nuevo comentario"
                        else:
                            reason = "actualizado (otros cambios)"
                        
                    # Si hay motivo, alerta y actualiza estado
                if reason:
                    # Prepara card/texto
                    card, plain = format_issue(issue, reason)
                    
                    if TEAMS_WEBHOOK_URL:
                        try:
                            print(card)
                            send_to_teams(card)
                            print(f"[{datetime.now().isoformat(timespec='seconds')}] Aviso Teams: {key} — {reason}")
                        except Exception as te:
                            print(f"[ERROR] Envío a Teams falló ({key}): {te}")
                            print(plain)
                    else:
                        #local_alarm(plain)
                        print(plain, flush=True)
                        print("alerta")
                        
                    alerts += 1
                        
                # Actualiza el registro de estado (incluso si no hubo motivo esta vez)
                if last_comment_updated is None:
                    # Si no lo obtuvo, conserva el anterior
                    last_comment_updated = (prev or {}).get('last_comment', '')
                    seen[key] = {
                        "updated": updated,
                        "priority": priority,
                        "last_comment": last_comment_updated
                    }
                    
            if alerts == 0:
                print(f"[{datetime.now().isoformat(timespec='seconds')}] Sin novedades.")

        except Exception as e:
            print(f"[ERROR] {e}")


if __name__ == "__main__":
    main()
