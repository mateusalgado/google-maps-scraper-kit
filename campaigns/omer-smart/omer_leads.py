#!/usr/bin/env python3
"""Lista B2B de compradores para máquinas de remoção de tatuagem (Omer Smart) — Grande Florianópolis.

Um comando, uma lista final:
    python3 campaigns/omer-smart/omer_leads.py

O que faz:
1. Lê queries.txt (e queries-extras.txt com --extras) e agrupa as buscas por cidade.
2. Roda UM job por cidade no motor local (coordenadas da própria cidade), um de cada vez.
   Cada cidade concluída fica salva em out/raw/, então se cair no meio é só rodar de novo.
3. Junta tudo, remove duplicados (place_id) e descarta o que estiver fora da região.
4. Busca e-mails nos sites (só depois de deduplicar, para visitar cada site uma vez),
   incluindo páginas /contato e /fale-conosco, e opcionalmente Instagram/Facebook.
5. Classifica cada empresa por segmento, dá uma nota de 0 a 100 e gera link de WhatsApp.

Saída (em campaigns/omer-smart/out/):
    omer-leads-todos.csv          todas as empresas da região, ordenadas pela nota
    omer-leads-prioritarios.csv   só o perfil comprador, com telefone ou e-mail

Opções úteis:
    --depth 3            quanto rolar em cada busca (padrão 3; 5 = mais resultados, mais lento)
    --extras             inclui os termos de queries-extras.txt
    --cities "Florianópolis,São José"   roda só essas cidades
    --no-socials         pula Instagram/Facebook (mais rápido)
    --fresh              ignora o cache em out/raw/ e refaz as buscas
    --proxy URL          proxy para o motor (repetível); recomendado em execuções grandes
    --rebuild            não busca nada no Google: só reprocessa o cache de out/raw/
"""
import argparse, csv, importlib.util, io, json, math, os, re, subprocess, sys, time, unicodedata, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir))
_spec = importlib.util.spec_from_file_location("scrape", os.path.join(KIT, "scripts", "scrape.py"))
scrape = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scrape)

# Coordenadas fixas (centro de cada município): sem depender de geocodificação externa.
CITIES = {
    "Florianópolis":             ("-27.5954", "-48.5480"),
    "São José":                  ("-27.6136", "-48.6366"),
    "Palhoça":                   ("-27.6455", "-48.6697"),
    "Biguaçu":                   ("-27.4942", "-48.6556"),
    "Santo Amaro da Imperatriz": ("-27.6853", "-48.7786"),
    "Governador Celso Ramos":    ("-27.3147", "-48.5589"),
    "Antônio Carlos":            ("-27.5181", "-48.7664"),
    "Águas Mornas":              ("-27.6939", "-48.8236"),
    "São Pedro de Alcântara":    ("-27.5664", "-48.8047"),
}
# Caixa da Grande Florianópolis (sul, norte, oeste, leste) para descartar resultados de fora.
REGION = (-27.95, -27.15, -49.05, -48.30)

# Segmento -> (regex sobre nome + categoria sem acento, pontos base). A primeira que casar vale.
SEGMENTS = [
    ("Já faz remoção / laser",          r"remoc|despigment|clarea|laser|q-?switch|picos|tattoo remov", 60),
    ("Dermatologia / biomedicina",      r"dermato|biomedic|harmoniza|cirurgi|plastic|medic|injetav|botox", 50),
    ("Cursos / escolas",                r"curso|escola|treinamento|capacitac|ensino|school|academy", 35),
    ("Equipamentos / locação / revenda", r"equipament|locac|locador|aluguel|assistencia tecnica|distribuid|fornecedor"
                                         r"|materia(l|is)|suprimento|loja de (produtos|artigos)|supplier|equipment", 35),
    ("Micropigmentação / sobrancelha",  r"micropig|dermopig|sobrancel|microblad|brow|lash|cilios", 40),
    ("Clínica de estética",             r"estetic|depila|spa\b|beleza|rejuven|corporal|facial|beauty|aesthetic", 40),
    ("Estúdio de tatuagem",             r"tatu|tattoo|\bink\b|piercing", 30),
]
OTHER = "Outro (fora do perfil)"
# Falsos positivos comuns: "clareamento dental", "corte a laser", "laser tag".
EXCLUDE = r"odonto|dentist|dental|corte a laser|gravac|laser tag|veterin|oficina|automot"
FIELDS = ["prioridade", "nota", "segmento", "nome", "categoria", "cidade", "telefone", "whatsapp",
          "email", "site", "instagram", "facebook", "endereco", "avaliacao", "num_avaliacoes", "link_maps"]


def plain(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def load_queries(paths):
    """-> {cidade: [buscas]}, mantendo a ordem do arquivo e da lista CITIES."""
    by_city = {c: [] for c in CITIES}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                q = line.strip()
                if not q or q.startswith("#"):
                    continue
                city = next((c for c in CITIES if q.endswith(" em " + c)), None)
                if city is None:
                    print(f"  ⚠ cidade não reconhecida, ignorando: {q}")
                elif q not in by_city[city]:
                    by_city[city].append(q)
    return {c: qs for c, qs in by_city.items() if qs}


def ensure_engine():
    try:
        scrape.req("GET", "/api/v1/jobs"); return
    except Exception:
        pass
    print("▶ Motor desligado. Subindo com 'docker compose up -d'…")
    try:
        subprocess.run(["docker", "compose", "up", "-d"], cwd=KIT, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"✗ Não consegui iniciar o Docker ({e}). Abra o Docker Desktop e rode de novo.")
    for _ in range(30):
        time.sleep(3)
        try:
            scrape.req("GET", "/api/v1/jobs"); print("  motor no ar."); return
        except Exception:
            pass
    sys.exit(f"✗ Motor não respondeu em {scrape.BASE}. Veja SETUP.md.")


def run_city(city, queries, a):
    """Um job por cidade. Retorna as linhas brutas do CSV ou [] se falhar."""
    lat, lon = CITIES[city]
    max_time = max(900, 90 * len(queries) * a.depth // 3)
    body = {"name": f"omer-{plain(city).replace(' ', '-')}", "keywords": queries, "lang": "pt-BR",
            "zoom": 14, "lat": lat, "lon": lon, "radius": 15000, "fast_mode": False,
            "depth": a.depth, "email": False, "max_time": max_time}
    if a.proxy:
        body["proxies"] = a.proxy
    try:
        _, raw = scrape.req("POST", "/api/v1/jobs", body)
        job_id = json.loads(raw)["id"]
    except (urllib.error.HTTPError, KeyError, ValueError) as e:
        print(f"  ✗ não criou o job: {e}"); return []
    start, status = time.time(), None
    while time.time() - start < max_time + 600:
        try:
            status = json.loads(scrape.req("GET", f"/api/v1/jobs/{job_id}")[1]).get("Status")
        except Exception:
            status = "?"
        print(f"\r  job {job_id[:8]}: {status:<8} {int(time.time() - start) // 60} min", end="", flush=True)
        if status in ("ok", "failed"):
            break
        time.sleep(15)
    print()
    if status != "ok":
        print("  ✗ job não terminou bem (pode ser bloqueio temporário do Google: espere ou use --proxy).")
        return []
    raw = scrape.req("GET", f"/api/v1/jobs/{job_id}/download")[1]
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))


def in_region(r):
    try:
        lat, lon = float(r.get("latitude")), float(r.get("longitude"))
    except (TypeError, ValueError):
        return bool(re.search(r"\bSC\b|santa catarina", r.get("address", ""), re.I))
    s, n, w, e = REGION
    return s <= lat <= n and w <= lon <= e


def city_of(address):
    a = plain(address)
    return next((c for c in sorted(CITIES, key=len, reverse=True) if plain(c) in a), "")


def whatsapp(phone):
    """Celular brasileiro (DDD + 9 dígitos começando em 9) -> link wa.me; fixo -> ''."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    d = d.lstrip("0")
    return f"https://wa.me/55{d}" if len(d) == 11 and d[2] == "9" else ""


def classify(r):
    text = plain(f"{r.get('title', '')} {r.get('category', '')}")
    if re.search(EXCLUDE, text):
        return OTHER, 5
    return next(((name, base) for name, rx, base in SEGMENTS if re.search(rx, text)), (OTHER, 5))


def to_lead(r):
    seg, base = classify(r)
    try:
        reviews = int(float(r.get("review_count") or 0))
    except ValueError:
        reviews = 0
    try:
        rating = float(r.get("review_rating") or 0)
    except ValueError:
        rating = 0.0
    lead = {"segmento": seg, "nome": r.get("title", ""), "categoria": r.get("category", ""),
            "cidade": city_of(r.get("address", "")), "telefone": r.get("phone", ""),
            "whatsapp": whatsapp(r.get("phone", "")), "email": r.get("emails", ""),
            "site": r.get("website", ""), "instagram": "", "facebook": "",
            "endereco": r.get("address", ""), "avaliacao": r.get("review_rating", ""),
            "num_avaliacoes": reviews or "", "link_maps": r.get("link", "")}
    lead["_base"], lead["_reviews"], lead["_rating"] = base, reviews, rating
    return lead


def score(l):
    s = l["_base"] + min(20, round(6 * math.log10(1 + l["_reviews"])))
    s += 3 if l["_rating"] >= 4.5 else 0
    s += 5 * bool(l["telefone"]) + 5 * bool(l["whatsapp"]) + 5 * bool(l["email"])
    s += 3 * bool(l["site"]) + 2 * bool(l["instagram"])
    return min(100, s)


def email_pass_ptbr(leads, workers=8):
    """Segunda chance para sites brasileiros: /contato, /fale-conosco, /contatos, /sobre."""
    todo = [l for l in leads if l["site"] and not l["email"]]
    def work(l):
        for path in ("/contato", "/fale-conosco", "/contatos", "/sobre"):
            found = scrape.extract_emails(scrape._fetch_html(l["site"].rstrip("/") + path, timeout=8), l["site"])
            if found:
                l["email"] = ", ".join(found); return
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, todo))


def write(path, rows):
    # ';' + BOM: abre direto no Excel em português; o Google Sheets também detecta.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter=";", extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Lista B2B de compradores de laser de remoção de tatuagem.")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--extras", action="store_true", help="inclui queries-extras.txt")
    ap.add_argument("--cities", help="lista separada por vírgula (padrão: todas)")
    ap.add_argument("--no-socials", dest="socials", action="store_false")
    ap.add_argument("--fresh", action="store_true", help="ignora o cache de out/raw/")
    ap.add_argument("--rebuild", action="store_true", help="só reprocessa out/raw/, sem buscar")
    ap.add_argument("--proxy", action="append", help="socks5://user:pass@host:port (repetível)")
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    a = ap.parse_args()

    files = [os.path.join(HERE, "queries.txt")] + ([os.path.join(HERE, "queries-extras.txt")] if a.extras else [])
    by_city = load_queries(files)
    if a.cities:
        wanted = {plain(c.strip()) for c in a.cities.split(",")}
        by_city = {c: q for c, q in by_city.items() if plain(c) in wanted}
    if not by_city:
        sys.exit("✗ Nenhuma busca para rodar.")
    raw_dir = os.path.join(a.out, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    total = sum(map(len, by_city.values()))
    print(f"▶ {total} buscas em {len(by_city)} cidades, depth={a.depth}.")
    if not a.rebuild:
        print("⚠️  Execução grande: sem proxy, o Google pode limitar seu IP por algumas horas (não afeta sua conta).\n"
              "    Se uma cidade falhar, espere e rode de novo: as cidades prontas ficam salvas em out/raw/.")
        ensure_engine()

    rows = []
    for i, (city, queries) in enumerate(by_city.items(), 1):
        cache = os.path.join(raw_dir, plain(city).replace(" ", "-") + ".json")
        if os.path.exists(cache) and not a.fresh:
            with open(cache, encoding="utf-8") as f:
                got = json.load(f)
            print(f"▶ [{i}/{len(by_city)}] {city}: {len(got)} do cache")
        elif a.rebuild:
            print(f"▶ [{i}/{len(by_city)}] {city}: sem cache, pulando"); continue
        else:
            print(f"▶ [{i}/{len(by_city)}] {city}: {len(queries)} buscas")
            got = run_city(city, queries, a)
            if got:
                with open(cache, "w", encoding="utf-8") as f:
                    json.dump(got, f, ensure_ascii=False)
            print(f"  {len(got)} resultados")
        rows += got

    rows = scrape.dedupe(rows)
    kept = [r for r in rows if in_region(r)]
    print(f"▶ {len(rows)} empresas únicas, {len(rows) - len(kept)} fora da região descartadas.")
    leads = [to_lead(r) for r in kept]
    if not leads:
        sys.exit("✗ Nenhum resultado. Confira se o motor está rodando e se o Google não bloqueou o IP.")

    print("▶ Procurando e-mails nos sites…")
    as_engine = [{"website": l["site"], "emails": l["email"]} for l in leads]
    scrape.enrich_emails(as_engine)
    for l, r in zip(leads, as_engine):
        l["email"] = r["emails"]
    email_pass_ptbr(leads)
    if a.socials:
        print("▶ Procurando Instagram/Facebook nos sites…")
        soc = [{"website": l["site"]} for l in leads]
        scrape.enrich_socials(soc)
        for l, r in zip(leads, soc):
            l["instagram"], l["facebook"] = r["instagram"], r["facebook"]

    for l in leads:
        l["nota"] = score(l)
        l["prioridade"] = "A" if l["nota"] >= 70 else "B" if l["nota"] >= 50 else "C"
    leads.sort(key=lambda l: -l["nota"])
    top = [l for l in leads if l["segmento"] != OTHER and (l["telefone"] or l["email"])]
    write(os.path.join(a.out, "omer-leads-todos.csv"), leads)
    write(os.path.join(a.out, "omer-leads-prioritarios.csv"), top)

    print(f"\n✓ {len(leads)} empresas na região, {len(top)} no perfil comprador com contato.")
    print(f"  com telefone {sum(bool(l['telefone']) for l in leads)}, WhatsApp {sum(bool(l['whatsapp']) for l in leads)}, "
          f"e-mail {sum(bool(l['email']) for l in leads)}, Instagram {sum(bool(l['instagram']) for l in leads)}")
    print("  por segmento:")
    for name in [s[0] for s in SEGMENTS] + [OTHER]:
        seg = [l for l in leads if l["segmento"] == name]
        if seg:
            a_ = sum(l["prioridade"] == "A" for l in seg)
            print(f"    {len(seg):>5}  {name}  (prioridade A: {a_})")
    print("  top 10:")
    for l in top[:10]:
        print(f"    {l['nota']:>3}  {l['nome']} | {l['cidade'] or '—'} | {l['telefone'] or l['email']}")
    print(f"  salvo em {a.out}/omer-leads-prioritarios.csv e omer-leads-todos.csv")


if __name__ == "__main__":
    main()
