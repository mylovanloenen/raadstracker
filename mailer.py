"""
HTML-mail samenstellen en versturen via Resend.
"""

import logging
import os
from datetime import date

import resend
from jinja2 import Environment, BaseLoader

logger = logging.getLogger(__name__)

MAIL_TEMPLATE = """<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<style>
  body { font-family: Arial, sans-serif; font-size: 14px; color: #222; max-width: 700px; margin: 0 auto; }
  h1 { color: #003082; border-bottom: 2px solid #003082; padding-bottom: 8px; }
  h2 { color: #333; font-size: 16px; margin-top: 24px; border-left: 4px solid; padding-left: 8px; }
  h2.nadert { border-color: #e67e00; }
  h2.verstreken { border-color: #c0392b; }
  h2.ruim-over { border-color: #7b241c; }
  h2.nieuw { border-color: #1a7a4a; }
  .item { margin: 12px 0; padding: 10px 12px; background: #f9f9f9; border-radius: 4px; }
  .item-title { font-weight: bold; }
  .item-meta { color: #666; font-size: 12px; margin-top: 4px; }
  .item-link { color: #003082; }
  .leeg { color: #999; font-style: italic; }
  .termijn-datum { font-weight: bold; }
  .tag { display: inline-block; background: #e0e8f0; padding: 1px 6px; border-radius: 3px; font-size: 11px; margin-right: 4px; }
</style>
</head>
<body>
<h1>Raadstracker — {{ naam }} — {{ datum }}</h1>

{% if buckets.nieuw %}
<h2 class="nieuw">Nieuw vandaag (top {{ buckets.nieuw|length }})</h2>
{% for item in buckets.nieuw %}
<div class="item">
  <div class="item-title"><a class="item-link" href="{{ item['bron_url'] }}">{{ item['titel'] }}</a></div>
  <div class="item-meta">
    {{ item['type']|replace('_', ' ')|title }} &bull; {{ item['indiener'] or '—' }}
    {% if item['relevantie_tags'] %} &bull; <span class="tag">{{ item['relevantie_tags'] }}</span>{% endif %}
    &bull; ingediend {{ item['datum_ingediend'] or '?' }}
  </div>
</div>
{% endfor %}
{% endif %}

{% if buckets.nadert %}
<h2 class="nadert">Termijn nadert (≤ 7 dagen) — {{ buckets.nadert|length }} item(s)</h2>
{% for item in buckets.nadert %}
<div class="item">
  <div class="item-title"><a class="item-link" href="{{ item['bron_url'] }}">{{ item['titel'] }}</a></div>
  <div class="item-meta">
    {{ item['type']|replace('_', ' ')|title }} &bull; termijn: <span class="termijn-datum">{{ item['termijn_einde'] }}</span>
    {% if item['relevantie_tags'] %} &bull; <span class="tag">{{ item['relevantie_tags'] }}</span>{% endif %}
  </div>
</div>
{% endfor %}
{% else %}
<p class="leeg">Geen items met naderende termijn.</p>
{% endif %}

{% if buckets.verstreken %}
<h2 class="verstreken">Termijn verstreken (0-14 dagen) — {{ buckets.verstreken|length }} item(s)</h2>
{% for item in buckets.verstreken %}
<div class="item">
  <div class="item-title"><a class="item-link" href="{{ item['bron_url'] }}">{{ item['titel'] }}</a></div>
  <div class="item-meta">
    Termijn was: <span class="termijn-datum">{{ item['termijn_einde'] }}</span>
    {% if item['afdoening_notitie'] %}<br>{{ item['afdoening_notitie'] }}{% endif %}
  </div>
</div>
{% endfor %}
{% else %}
<p class="leeg">Geen items recent over termijn.</p>
{% endif %}

{% if buckets.ruim_over %}
<h2 class="ruim-over">Ruim over termijn (&gt;14 dagen) — {{ buckets.ruim_over|length }} item(s)</h2>
{% for item in buckets.ruim_over %}
<div class="item">
  <div class="item-title"><a class="item-link" href="{{ item['bron_url'] }}">{{ item['titel'] }}</a></div>
  <div class="item-meta">Termijn was: <span class="termijn-datum">{{ item['termijn_einde'] }}</span></div>
</div>
{% endfor %}
{% else %}
<p class="leeg">Geen items ruim over termijn.</p>
{% endif %}

<p style="margin-top:32px;color:#999;font-size:11px;">
  Raadstracker &bull; Automatisch gegenereerd
</p>
</body>
</html>"""


def render_mail(buckets: dict, naam: str = "") -> str:
    env = Environment(loader=BaseLoader())
    tmpl = env.from_string(MAIL_TEMPLATE)
    return tmpl.render(
        naam=naam,
        datum=date.today().strftime("%-d %B %Y"),
        buckets={k: [dict(row) for row in v] for k, v in buckets.items()},
    )


def send_mail(html: str, recipient: str, naam: str = "") -> None:
    resend.api_key = os.environ["RESEND_API_KEY"]
    from_email = os.environ.get("FROM_EMAIL", "noreply@raadstracker.nl")
    from_name = os.environ.get("FROM_NAME", "Raadstracker")

    resend.Emails.send({
        "from": f"{from_name} <{from_email}>",
        "to": [recipient],
        "subject": f"Raadstracker {date.today().strftime('%-d %b')} — {naam}",
        "html": html,
    })

    logger.info(f"Mail verstuurd naar {recipient} ({naam})")
