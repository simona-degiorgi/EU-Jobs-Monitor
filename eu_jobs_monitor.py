"""EU Careers job monitor: sends an email when a new FG III vacancy is published.

Usage:
    python eu_jobs_monitor.py --dry-run   # print results, no email, nothing saved
    python eu_jobs_monitor.py             # send the email if there are new vacancies

Environment variables needed to send mail: GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO
"""
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

GRADES = ["FG III"]  # grades to monitor (exact match on a single grade)

BASE = "https://eu-careers.europa.eu"
RSS_URL = BASE + "/node/244/rss_en"
LIST_URL = BASE + "/jobs_en?page={page}"
MAX_PAGES = 30
SEEN_FILE = Path(__file__).with_name("seen.json")
USER_AGENT = "Mozilla/5.0 (compatible; EUJobsMonitor/1.0; personal use)"


def fetch(url):
    time.sleep(1)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def job_key(link):
    path = urlparse(link).path
    return re.sub(r"_en$", "", path)


def text(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def matches(grades):
    return any(g.strip() in GRADES for g in grades)


def from_rss():
    root = ET.fromstring(fetch(RSS_URL).encode("utf-8"))
    items = root.findall("./channel/item")
    jobs = {}
    for it in items:
        cats = {}
        for c in it.findall("category"):
            cats.setdefault(c.get("domain"), []).append((c.text or "").strip())
        grades = cats.get("Grade", [])
        if not matches(grades):
            continue
        link = it.findtext("link")
        title = cats.get("Job title - standard", [it.findtext("title")])[0]
        jobs[job_key(link)] = {
            "title": title,
            "link": link,
            "institution": ", ".join(cats.get("Institution / Agency", [])),
            "city": ", ".join(cats.get("City", [])),
            "grades": ", ".join(grades),
            "contract": ", ".join(cats.get("Contract type", [])),
            "deadline": None,
        }
    return len(items), jobs


def from_listing():
    jobs, cards_total = {}, 0
    for page in range(MAX_PAGES):
        page_html = fetch(LIST_URL.format(page=page))
        cards = page_html.split('class="eujmp-vacancy-result"')[1:]
        if not cards:
            break
        cards_total += len(cards)
        for card in cards:
            m = re.search(
                r'<li>([^<]*)</li><li><time datetime="([^"]+)"'
                r'.*?<a href="([^"]+)"[^>]*eujmp-vacancy-result__title">([^<]*)</a>'
                r'<ul class="eujmp-vacancy-result__bottom-meta">(.*?)</ul>',
                card, re.S)
            if not m:
                continue
            inst, deadline, href, title, bottom = m.groups()
            lis = [text(x) for x in re.findall(r"<li>(.*?)</li>", bottom, re.S)]
            grade = next((x for x in lis if re.fullmatch(r"(FG|AD|AST)\b.*", x)), "")
            if not matches([grade]):
                continue
            link = BASE + href
            jobs[job_key(link)] = {
                "title": html.unescape(title),
                "link": link.split("?")[0],
                "institution": html.unescape(inst),
                "city": lis[-1] if lis else "",
                "grades": grade,
                "contract": lis[0] if lis else "",
                "deadline": deadline[:10],
            }
    return cards_total, jobs


def fill_deadline(job):
    try:
        page = fetch(job["link"])
        m = re.search(r"Application deadline:</strong>\s*<time datetime=\"([^\"]+)\"[^>]*>([^<]*)", page)
        if m:
            job["deadline"] = m.group(1)[:10]
            job["deadline_text"] = m.group(2).strip()
    except Exception as e:  # the deadline is just an extra
        print(f"  (deadline not read for {job['link']}: {e})")


def build_mail(new_jobs, first_run):
    rows = []
    for j in new_jobs:
        deadline = j.get("deadline_text") or j.get("deadline") or "n/a"
        rows.append(
            f"<li><p><b><a href=\"{j['link']}\">{html.escape(j['title'])}</a></b><br>"
            f"{html.escape(j['institution'])} &middot; {html.escape(j['city'])}<br>"
            f"{html.escape(j['contract'])} &middot; {html.escape(j['grades'])}<br>"
            f"Deadline: {html.escape(deadline)}</p></li>")
    grade = " / ".join(GRADES)
    intro = (f"{grade} vacancies currently open:" if first_run
             else f"New {grade} vacancies published:")
    body = f"<p>{intro}</p><ul>{''.join(rows)}</ul><p><a href=\"{BASE}/jobs_en\">All vacancies</a></p>"
    n = len(new_jobs)
    subject = f"[EU Careers] {n} {grade} {'vacancies' if n != 1 else 'vacancy'}" + (
        " open (first run)" if first_run else " new")
    return subject, body


def send_mail(subject, body):
    user, pwd, to = (os.environ.get(k) for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO"))
    if not (user and pwd and to):
        sys.exit("Missing GMAIL_USER / GMAIL_APP_PASSWORD / MAIL_TO")
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(re.sub(r"<[^>]+>", "", body.replace("</p>", "\n").replace("<br>", "\n")), "plain", "utf-8"))
    msg.attach(MIMEText(body, "html", "utf-8"))
    user, pwd = user.strip(), "".join(pwd.split())  # strip spaces/newlines from the app password
    recipients = [a.strip() for a in to.split(",")]
    last_error = None
    for attempt in range(3):
        for port in (465, 587):
            try:
                if port == 465:
                    server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
                else:
                    server = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
                    server.starttls()
                with server:
                    server.login(user, pwd)
                    server.sendmail(user, recipients, msg.as_string())
                return
            except (smtplib.SMTPException, OSError) as e:
                last_error = e
                print(f"Send failed (port {port}, attempt {attempt + 1}): {e}")
        time.sleep(10)
    sys.exit(f"ERROR: could not send the email: {last_error}")


def main():
    dry = "--dry-run" in sys.argv
    n_items, jobs = from_rss()
    n_cards, listed = from_listing()
    print(f"RSS: {n_items} vacancies, listing: {n_cards} cards")
    if n_items == 0 and n_cards == 0:
        sys.exit("ERROR: no vacancies found, the site may have changed")
    for k, v in listed.items():
        jobs.setdefault(k, v)

    seen = json.loads(SEEN_FILE.read_text("utf-8")) if SEEN_FILE.exists() else {}
    first_run = not seen
    new = {k: v for k, v in jobs.items() if k not in seen}
    print(f"Found {len(jobs)} {GRADES} vacancies, new: {len(new)}")
    for j in new.values():
        if not j.get("deadline"):
            fill_deadline(j)
        print(f" - {j['title']} | {j['institution']} | {j['grades']} | {j.get('deadline')} | {j['link']}")

    if not new:
        return
    if dry:
        print("(dry-run: no email, nothing saved)")
        return
    subject, body = build_mail(sorted(new.values(), key=lambda j: j.get("deadline") or "9"), first_run)
    send_mail(subject, body)
    seen.update({k: {"title": v["title"], "link": v["link"]} for k, v in new.items()})
    SEEN_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False), "utf-8")
    print("Email sent and seen.json updated")


if __name__ == "__main__":
    main()
