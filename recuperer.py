#!/usr/bin/env python3
"""Cours de clôture quotidiens d'une liste d'ISIN.

Entrée  : isins.txt (un ISIN par ligne ; « # » commente).
Sorties : donnees/derniers.json   {isin: {cours, devise, date, source, symbole}}
          donnees/historique-AAAA.csv   date;isin;cours;devise;source (une ligne par cours nouveau)
          donnees/symboles.json   ISIN → symbole trouvé (cache)
          donnees/rapport.json    trouvés, manquants, à quelle heure
Sources, dans l'ordre : Financial Times (fiche fonds par ISIN, devises converties au cours BCE ; puis sa recherche, pour les
ETF et les titres cotés), VDOS / quefondos.com (fiche par ISIN), Yahoo Finance en dernier recours. Chaque cours vient d'une
page qui porte l'ISIN cherché et date de moins de 10 jours ; sinon l'ISIN reste « manquant », jamais un mauvais cours.
"""
import csv
import datetime
import json
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
D = ROOT / "donnees"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
      "Accept": "application/json,text/html;q=0.9,*/*;q=0.8", "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def get(url, tries=2):
    """Une page ou un JSON ; deux essais."""
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # réseau, 404, 429
            err = e
            time.sleep(2 + 3 * k)
    raise err


def yahoo_cours(symbole):
    """Dernière clôture d'un symbole Yahoo : (cours, devise, date) ou None."""
    j = json.loads(get(f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbole)}?range=15d&interval=1d", tries=1))
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        return None
    r = res[0]
    ts, closes = r.get("timestamp") or [], ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    for t, c in reversed(list(zip(ts, closes))):
        if c is not None:
            return round(float(c), 6), (r.get("meta") or {}).get("currency"), datetime.datetime.utcfromtimestamp(t).date().isoformat()
    return None


def yahoo(isin, cache):
    """Cherche l'ISIN sur Yahoo ; retient une cotation en euros, sinon la première trouvée."""
    cands = [cache[isin]] if cache.get(isin, {}).get("source") == "yahoo" else []
    if not cands:
        j = json.loads(get(f"https://query2.finance.yahoo.com/v1/finance/search?q={isin}&quotesCount=8&newsCount=0", tries=1))
        cands = [{"source": "yahoo", "symbole": q["symbol"]} for q in (j.get("quotes") or [])
                 if q.get("symbol") and q.get("quoteType") in ("ETF", "MUTUALFUND", "EQUITY", "INDEX", "MONEYMARKET", "FUTURE")]
    secours = None
    for c in cands[:5]:
        try:
            r = yahoo_cours(c["symbole"])
        except Exception:
            continue
        if not r:
            continue
        if r[1] == "EUR":
            return {**c, "cours": r[0], "devise": r[1], "date": r[2]}
        secours = secours or {**c, "cours": r[0], "devise": r[1], "date": r[2]}
        time.sleep(0.4)
    return secours


BCE = {}


def bce():
    """Cours de référence de la BCE du jour (devise → unités pour 1 euro)."""
    if not BCE:
        x = get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml")
        BCE.update({m.group(1): float(m.group(2)) for m in re.finditer(r"currency='([A-Z]{3})' rate='([\d.]+)'", x)})
    return BCE


def ft_fiche(kind, symbole, isin=None):
    """Cours d'une fiche du Financial Times (fonds, ETF ou titre), converti en euros au cours BCE si besoin.
    Trouvée par la recherche, la fiche doit porter l'ISIN cherché et un cours de moins de 10 jours : la recherche du FT
    rend parfois un autre titre (essai du 17/11/2026 : une action japonaise pour un produit structuré)."""
    h = get(f"https://markets.ft.com/data/{kind}/tearsheet/summary?s={urllib.parse.quote(symbole)}")
    if isin and isin not in h:
        return None
    m = re.search(r"Price \(([A-Z]{3})\)</span>\s*<span class=\"mod-ui-data-list__value\">([\d,]+\.?\d*)</span>", h)
    if not m:
        return None
    dev, cours = m.group(1), float(m.group(2).replace(",", ""))
    d = re.search(r"as of ([A-Z][a-z]{2} \d{1,2} \d{4})", h)
    date = datetime.datetime.strptime(d.group(1), "%b %d %Y").date().isoformat() if d else None
    if isin and (not date or (datetime.date.today() - datetime.date.fromisoformat(date)).days > 10):
        return None
    r = {"source": "ft", "symbole": symbole, "kind": kind, "cours": cours, "devise": dev, "date": date}
    if dev in ("GBX", "GBp"):
        dev, cours = "GBP", cours / 100
    if dev != "EUR":
        taux = bce().get(dev)
        if not taux:
            return None
        r.update({"cours": round(cours / taux, 6), "devise": "EUR", "coursOrigine": cours, "deviseOrigine": dev, "tauxBCE": taux})
    return r


def frais(date):
    """Vrai si la date (AAAA-MM-JJ) a moins de 10 jours."""
    return bool(date) and (datetime.date.today() - datetime.date.fromisoformat(date)).days <= 10


def quefondos(isin, cache=None):
    """VDOS / quefondos.com : fiche du fonds par ISIN, « Valor liquidativo » et sa « Fecha » (nombres à l'espagnole)."""
    h = get(f"https://www.quefondos.com/es/fondos/ficha/index.html?isin={isin}")
    if isin not in h:
        return None
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h))
    m = re.search(r"Valor liquidativo\s*:?\s*([\d.]+,\d+)\s*([A-Z]{3})", t)
    if not m:
        return None
    d = re.search(r"Fecha\s*:?\s*(\d{2})/(\d{2})/(\d{4})", t[m.end():])
    date = f"{d.group(3)}-{d.group(2)}-{d.group(1)}" if d else None
    if not frais(date):
        return None
    cours, dev = float(m.group(1).replace(".", "").replace(",", ".")), m.group(2)
    r = {"source": "quefondos", "symbole": isin, "cours": cours, "devise": dev, "date": date}
    if dev != "EUR":
        taux = bce().get(dev)
        if not taux:
            return None
        r.update({"cours": round(cours / taux, 6), "devise": "EUR", "coursOrigine": cours, "deviseOrigine": dev, "tauxBCE": taux})
    return r


def ft(isin, cache):
    """Financial Times : la fiche déjà trouvée, puis la fiche fonds par ISIN, puis la recherche (ETF, titres)."""
    c = cache.get(isin, {})
    if c.get("source") == "ft" and c.get("kind"):
        r = ft_fiche(c["kind"], c["symbole"], isin if c["kind"] != "funds" else None)
        if r:
            return r
    for dev in ("EUR", "USD", "GBP", "CHF"):
        r = ft_fiche("funds", f"{isin}:{dev}")
        if r:
            return r
    j = json.loads(get(f"https://markets.ft.com/data/searchapi/searchsecurities?query={isin}"))
    secs = ((j.get("data") or {}).get("security") or [])
    secs.sort(key=lambda x: (not str(x.get("symbol", "")).endswith(":EUR"), str(x.get("symbol", ""))))
    for sec in secs[:6]:
        sym, ac = sec.get("symbol"), str(sec.get("assetClass") or "").lower()
        kinds = ["etfs", "equities", "funds"] if "etf" in ac or "etc" in ac else ["equities", "etfs", "funds"]
        for kind in kinds:
            try:
                r = ft_fiche(kind, sym, isin)
            except Exception:
                r = None
            if r:
                return r
    return None


def main():
    isins = [l.split("#")[0].strip().upper() for l in (ROOT / "isins.txt").read_text().splitlines()]
    isins = sorted({i for i in isins if ISIN.match(i)})
    lire = lambda p, d: json.loads(p.read_text()) if p.exists() else d
    cache, derniers = lire(D / "symboles.json", {}), lire(D / "derniers.json", {})
    trouves, manquants = [], []
    for isin in isins:
        r = None
        for f in (lambda: ft(isin, cache), lambda: quefondos(isin), lambda: yahoo(isin, cache)):
            try:
                r = f()
            except Exception:
                r = None
            if r:
                break
        time.sleep(0.6)
        if not r:
            manquants.append(isin)
            continue
        cache[isin] = {k: r[k] for k in ("source", "symbole", "kind") if k in r}
        derniers[isin] = {k: r[k] for k in ("cours", "devise", "date", "source", "symbole", "coursOrigine", "deviseOrigine", "tauxBCE") if k in r}
        trouves.append(isin)
    an = datetime.date.today().year
    hist = D / f"historique-{an}.csv"
    deja = set()
    if hist.exists():
        with hist.open() as f:
            deja = {(row[0], row[1]) for row in csv.reader(f, delimiter=";") if len(row) > 1}
    with hist.open("a", newline="") as f:
        w = csv.writer(f, delimiter=";")
        if not deja:
            w.writerow(["date", "isin", "cours", "devise", "source"])
        for isin in trouves:
            c = derniers[isin]
            if c["date"] and (c["date"], isin) not in deja:
                w.writerow([c["date"], isin, c["cours"], c["devise"], c["source"]])
    (D / "symboles.json").write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True))
    (D / "derniers.json").write_text(json.dumps(derniers, ensure_ascii=False, indent=1, sort_keys=True))
    (D / "rapport.json").write_text(json.dumps({"at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                                                "suivis": len(isins), "trouves": len(trouves), "manquants": manquants},
                                               ensure_ascii=False, indent=1))
    print(f"{len(trouves)}/{len(isins)} cours trouvés ; manquants : {', '.join(manquants) or 'aucun'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
