
from functools import wraps
import os
import re

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
import httpx
from openai import APIConnectionError, OpenAI
from pydantic import BaseModel, Field


app = Flask(__name__)

database_url = os.environ.get("DATABASE_URL")

if not database_url:
    database_url = "sqlite:///jobs.db"

if database_url.startswith("postgres://"):
    database_url = database_url.replace(
        "postgres://",
        "postgresql://",
        1,
    )

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
    "pool_size": 5,
    "max_overflow": 2,
    "pool_timeout": 30,
}

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "change-cette-cle-avant-le-deploiement",
)

# Autoriser les imports massifs contenant beaucoup de texte.
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 12 * 1024 * 1024
db = SQLAlchemy(app)

# Identifiants administrateur par défaut.
# Ils seront remplacés par des variables d'environnement sur Render.
ADMIN_USERNAME = os.environ.get(
    "ADMIN_USERNAME",
    "admin",
)

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "change-moi",
)


# ============================================================
# MODÈLE DE BASE DE DONNÉES
# ============================================================

class Job(db.Model):
    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    reference = db.Column(
        db.String(40),
        unique=True,
        nullable=False,
    )

    title = db.Column(
        db.String(150),
        nullable=False,
    )

    city = db.Column(
        db.String(100),
        nullable=False,
    )

    contract_type = db.Column(
        db.String(100),
        default="",
    )

    schedule = db.Column(
        db.String(250),
        default="",
    )

    hebrew_level = db.Column(
        db.String(150),
        default="",
    )

    experience = db.Column(
        db.String(200),
        default="",
    )

    description = db.Column(
        db.Text,
        nullable=False,
    )

    requirements = db.Column(
        db.Text,
        default="",
    )

    advantages = db.Column(
        db.Text,
        default="",
    )

    form_link = db.Column(
        db.String(500),
        nullable=False,
    )

    published = db.Column(
        db.Boolean,
        default=True,
        nullable=False,
    )

    shuttle_available = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
    )

    shuttle_cities = db.Column(
        db.String(500),
        default="",
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


# ============================================================
# MODÈLE DES PUBLICITÉS / PROFESSIONNELS
# ============================================================

class Advertisement(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    company_name = db.Column(
        db.String(150),
        nullable=False,
    )

    professional_name = db.Column(
        db.String(150),
        nullable=False,
    )

    activity = db.Column(
        db.String(120),
        nullable=False,
    )

    specialty = db.Column(
        db.String(200),
        default="",
    )

    address = db.Column(
        db.String(250),
        default="",
    )

    phone = db.Column(
        db.String(100),
        default="",
    )

    email = db.Column(
        db.String(150),
        default="",
    )

    website = db.Column(
        db.String(500),
        default="",
    )

    description = db.Column(
        db.Text,
        default="",
    )

    icon = db.Column(
        db.String(20),
        default="💼",
    )

    published = db.Column(
        db.Boolean,
        default=True,
        nullable=False,
    )

    display_order = db.Column(
        db.Integer,
        default=0,
        nullable=False,
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


# ============================================================
# FORMAT STRUCTURÉ ATTENDU DE L'INTELLIGENCE ARTIFICIELLE
# ============================================================

class ExtractedJob(BaseModel):
    reference: str = Field(
        description="Numéro exact du poste",
    )

    title: str = Field(
        description="Titre du poste en français",
    )

    city: str = Field(
        description=(
            "Ville ou région du poste exactement en hébreu, "
            "sans rue ni adresse précise"
        ),
    )
    contract_type: str = Field(
        default="",
        description="Temps plein, temps partiel ou type de contrat",
    )

    schedule: str = Field(
        default="",
        description="Jours et horaires en français",
    )

    hebrew_level: str = Field(
        default="",
        description="Niveau d'hébreu ou Non précisé",
    )

    experience: str = Field(
        default="",
        description="Expérience demandée ou Non précisée",
    )

    description: str = Field(
        description="Résumé clair des missions en français",
    )

    requirements: str = Field(
        default="",
        description="Exigences, une par ligne",
    )

    advantages: str = Field(
        default="",
        description=(
            "Salaire du salarié, primes du salarié, repas, "
            "transport et avantages, une information par ligne"
        ),
    )

    shuttle_available: bool = Field(
        default=False,
        description=(
            "True uniquement si une navette pour les salariés "
            "est clairement mentionnée dans l'annonce"
        ),
    )

    shuttle_cities: str = Field(
        default="",
        description=(
            "Villes de départ desservies par la navette, "
            "séparées par des virgules. Vide si aucune navette."
        ),
    )


# ============================================================
# PROTECTION DE L'ADMINISTRATION
# ============================================================

def admin_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_logged_in"):
            flash(
                "Connectez-vous pour accéder à l'administration.",
                "error",
            )

            return redirect(url_for("login"))

        return view_function(*args, **kwargs)

    return wrapped_view


# ============================================================
# DÉCOUPAGE DES ANNONCES
# ============================================================

def split_raw_jobs(raw_text: str) -> list[str]:
    """
    Sépare les annonces selon leur numéro de poste.

    Formats reconnus :
    משרה מספר: 29399
    svgמשרה מספר: 29399
    מספר משרה: 29399
    """

    text = raw_text.replace("\u00a0", " ")

    pattern = re.compile(
        r"(?="
        r"(?:\*{0,2})?"
        r"(?:svg)?"
        r"\s*"
        r"(?:משרה\s*מספר|מספר\s*משרה)"
        r"\s*:\s*"
        r"\d{4,}"
        r")",
        flags=re.IGNORECASE,
    )

    possible_blocks = pattern.split(text)

    blocks = []

    for block in possible_blocks:
        cleaned_block = block.strip()

        if not cleaned_block:
            continue

        contains_reference = re.search(
            r"(?:משרה\s*מספר|מספר\s*משרה)"
            r"\s*:\s*"
            r"\d{4,}",
            cleaned_block,
        )

        if contains_reference:
            blocks.append(cleaned_block)

    return blocks


def extract_reference(raw_job: str) -> str:
    match = re.search(
        r"(?:משרה\s*מספר|מספר\s*משרה)"
        r"\s*:\s*"
        r"(\d{4,})",
        raw_job,
    )

    if not match:
        return ""

    return match.group(1)


# ============================================================
# NETTOYAGE AVANT L'ENVOI À OPENAI
# ============================================================

def remove_forbidden_content(raw_job: str) -> str:
    """
    Supprime les données interdites avant même l'analyse par l'IA.
    """

    text = raw_job.replace("\u00a0", " ")

    # --------------------------------------------------------
    # Supprimer toute la section des commissions en hébreu
    # --------------------------------------------------------

    text = re.sub(
        r"(?:####?\s*)?"
        r"(?:svg)?"
        r"טווחי\s*עמלות"
        r".*?"
        r"(?="
        r"(?:\*\*)?"
        r"(?:svg)?"
        r"מיקומי\s*המשרה"
        r"|"
        r"(?:svg)?אירועים"
        r"|"
        r"(?:svg)?סניפים"
        r"|$"
        r")",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --------------------------------------------------------
    # Supprimer les sections d'instructions internes
    # --------------------------------------------------------

    text = re.sub(
        r"איך\s*מגייסים\s*:.*?"
        r"(?="
        r"####?\s*(?:svg)?תיאור\s*המשרה"
        r"|$"
        r")",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --------------------------------------------------------
    # Supprimer le tableau des lieux précis et contacts
    # --------------------------------------------------------

    text = re.sub(
        r"(?:\*\*)?"
        r"(?:svg)?"
        r"מיקומי\s*המשרה"
        r".*?"
        r"(?="
        r"(?:svg)?אירועים"
        r"|"
        r"(?:svg)?סניפים"
        r"|$"
        r")",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --------------------------------------------------------
    # Mots qui interdisent toute la ligne
    # --------------------------------------------------------

    forbidden_line_patterns = [
        r"טווחי\s*עמלות",
        r"עמלת",
        r"עמלות",
        r"שותף\s*פעיל",
        r"שותף\s*בכיר",
        r"שותף\s*מלא",
        r"שותף\s*בעל\s*הבית",
        r"JobTarget",
        r"איך\s*מגייסים",
        r"ימי\s*התחייבות",
        r"מגיע\s*מ-JobTarget",
        r"בהמתנה\s*מ-JobTarget",
        r"תצוגת\s*מפה",
        r"פוסט\s*משרה",
        r"סניפים",
        r"אירועים",
        r"ת\.רישום",
        r"שם\s*איש\s*קשר",
        r"מייל\s*איש\s*קשר",
    ]

    cleaned_lines = []

    for original_line in text.splitlines():
        line = original_line.strip()

        if not line:
            cleaned_lines.append("")
            continue

        forbidden = any(
            re.search(
                pattern,
                line,
                flags=re.IGNORECASE,
            )
            for pattern in forbidden_line_patterns
        )

        if forbidden:
            continue

        # Supprimer les noms d'entreprises israéliennes.
        # בע"מ correspond généralement à « société limitée ».
        if re.search(
            r"בע[\"״']?מ",
            line,
            flags=re.IGNORECASE,
        ):
            continue

        # Supprimer les lignes qui ne contiennent que SVG.
        simplified = re.sub(
            r"[*#_\s]",
            "",
            line,
        )

        if simplified.lower() in {
            "svg",
            "svgsvg",
            "svgsvgsvg",
            "svgsvgsvgsvg",
        }:
            continue

        cleaned_lines.append(original_line)

    text = "\n".join(cleaned_lines)

    # Supprimer les textes techniques SVG restants.
    text = re.sub(
        r"\bsvg\b",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Supprimer les e-mails.
    text = re.sub(
        r"[\w.+-]+\\?@[\w.-]+\.[A-Za-z]{2,}",
        "",
        text,
    )

    # Supprimer les numéros de téléphone israéliens.
    text = re.sub(
        r"(?:\+?972[-\s]?)?"
        r"0?"
        r"5\d"
        r"[-\s]?"
        r"\d{3}"
        r"[-\s]?"
        r"\d{4}",
        "",
        text,
    )

    # Supprimer les lignes vides répétées.
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    )

    return text.strip()


# ============================================================
# NETTOYAGE APRÈS LA RÉPONSE DE L'IA
# ============================================================

FORBIDDEN_PUBLIC_PATTERNS = [
    r"commission",
    r"commissions",
    r"commission estimée",
    r"commission de base",
    r"partenaire actif",
    r"partenaire senior",
    r"partenaire complet",
    r"partenaire propriétaire",
    r"versement en",
    r"versée? en \d+ fois",
    r"rémunération d'agence",
    r"rémunération du recruteur",
    r"prime de recrutement",
    r"עמלת",
    r"עמלות",
    r"שותף פעיל",
    r"שותף בכיר",
    r"שותף מלא",
    r"שותף בעל הבית",
    r"JobTarget",
]


def contains_forbidden_content(value: str) -> bool:
    if not value:
        return False

    return any(
        re.search(
            pattern,
            value,
            flags=re.IGNORECASE,
        )
        for pattern in FORBIDDEN_PUBLIC_PATTERNS
    )


def sanitize_public_text(value: str) -> str:
    """
    Dernière barrière de sécurité.

    Toute phrase contenant une commission ou une donnée interne
    est totalement supprimée.
    """

    if not value:
        return ""

    text = value.replace("\r", "\n")

    # Supprimer les e-mails.
    text = re.sub(
        r"[\w.+-]+\\?@[\w.-]+\.[A-Za-z]{2,}",
        "",
        text,
    )

    # Supprimer les téléphones.
    text = re.sub(
        r"(?:\+?972[-\s]?)?"
        r"0?"
        r"5\d"
        r"[-\s]?"
        r"\d{3}"
        r"[-\s]?"
        r"\d{4}",
        "",
        text,
    )

    # Découper le texte en petites phrases/blocs.
    parts = re.split(
        r"\n+|"
        r";+|"
        r"•+|"
        r"(?<=[.!?])\s+",
        text,
    )

    safe_parts = []

    for part in parts:
        cleaned_part = part.strip(" \t-–—:,.")

        if not cleaned_part:
            continue

        if contains_forbidden_content(cleaned_part):
            continue

        safe_parts.append(cleaned_part)

    cleaned_text = "\n".join(safe_parts)

    cleaned_text = re.sub(
        r"\n{3,}",
        "\n\n",
        cleaned_text,
    )

    return cleaned_text.strip()


def validate_no_forbidden_content(job: ExtractedJob) -> None:
    """
    Bloque complètement une offre si une commission est encore détectée.
    """

    fields_to_check = [
        job.title,
        job.city,
        job.contract_type,
        job.schedule,
        job.hebrew_level,
        job.experience,
        job.description,
        job.requirements,
        job.advantages,
        job.shuttle_cities,
    ]

    for value in fields_to_check:
        if contains_forbidden_content(value):
            raise ValueError(
                "Contenu interdit détecté : "
                "commission ou information interne."
            )


# ============================================================
# ANALYSE AVEC OPENAI
# ============================================================

def analyse_raw_job(raw_job: str) -> ExtractedJob:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError(
            "La variable OPENAI_API_KEY n'est pas configurée."
        )

    cleaned_raw_job = remove_forbidden_content(raw_job)

    timeout = httpx.Timeout(
        120.0,
        connect=30.0,
    )

    # trust_env=False évite qu'un proxy Windows, un VPN ou une variable
    # HTTP_PROXY/HTTPS_PROXY cassée soit utilisée par Python.
    http_client = httpx.Client(
        timeout=timeout,
        trust_env=False,
        follow_redirects=True,
    )

    client = OpenAI(
        api_key=api_key,
        timeout=timeout,
        max_retries=2,
        http_client=http_client,
    )

    system_prompt = """
Tu transformes une annonce d'emploi israélienne brute en fiche destinée à des candidats francophones.

RÈGLE ABSOLUE :

Les commissions de recrutement ne doivent JAMAIS apparaître.

Tu ne dois jamais écrire, traduire, résumer ou mentionner :

- commission estimée ;
- commission de base ;
- commission partenaire actif ;
- commission partenaire senior ;
- commission partenaire complet ;
- commission partenaire propriétaire ;
- rémunération d'agence ;
- rémunération du recruteur ;
- nombre de versements ;
- prime de recrutement ;
- עמלת ;
- עמלות ;
- שותף פעיל ;
- שותף בכיר ;
- שותף מלא ;
- שותף בעל הבית.

Attention :

- Une prime versée AU SALARIÉ peut être conservée.
- Une commission versée AU RECRUTEUR ou À L'AGENCE est interdite.

Tu dois également supprimer totalement :

- le nom de l'entreprise ;
- le nom des responsables ;
- les adresses e-mail ;
- les numéros de téléphone ;
- les adresses précises ;
- les numéros de rue ;
- les informations JobTarget ;
- les informations destinées aux recruteurs ;
- les commentaires internes ;
- les symboles et textes SVG.

Tu dois conserver uniquement :

- le numéro du poste ;
- un titre professionnel naturel en français ;
- la ville OU la région, conservée en hébreu ;
- le type de contrat ;
- les jours de travail ;
- les horaires ;
- le salaire ;
- les primes destinées au salarié ;
- les avantages ;
- les missions ;
- les exigences ;
- le niveau d'hébreu ;
- l'expérience ;
- les informations sur les navettes éventuelles.

RÈGLES POUR LE LIEU :

RÈGLES POUR LE LIEU :

- Le champ city doit toujours rester en hébreu.
- Le champ city peut contenir soit une ville, soit une région.
- Une région est une localisation valide.
- Ne traduis jamais le nom d'une ville.
- Ne traduis jamais le nom d'une région.
- Si une ville est indiquée, conserve uniquement la ville.
- Si seule une région est indiquée, conserve la région.
- Si une ville et une région sont indiquées, privilégie la ville.
- Exemples de régions valides :
  אזור המרכז
  אזור השרון
  אזור השפלה
  ירושלים והסביבה
  צפון
  דרום
  מרכז
- Si aucune ville ni région n'est présente, laisse le champ vide.
- N'écris jamais "Non précisé".
- N'invente jamais une localisation.
- Ne conserve jamais une rue ou une adresse.

RÈGLES POUR LE SALAIRE :

Le salaire est une information PRIORITAIRE.

Si un salaire est indiqué, il doit toujours être conservé.

Exemples :

10,000 ₪
10,000–12,000 ₪
55 ₪ לשעה

Les primes destinées au salarié doivent également être conservées.

RÈGLES POUR LES HORAIRES :

Les horaires sont prioritaires.

Conserve toujours :

- les jours travaillés ;
- l'heure de début ;
- l'heure de fin.

Exemple :

א׳–ה׳
07:30–17:00

RÈGLES POUR LES EXIGENCES :

Chaque exigence doit être sur une ligne différente.

RÈGLES POUR LES AVANTAGES :

Chaque avantage doit être sur une ligne différente.

RÈGLES POUR LES NAVETTES :

Si une navette est mentionnée :

- shuttle_available = true
- shuttle_cities doit contenir uniquement les villes de départ.

Ne mets jamais la ville du poste dans shuttle_cities.

Sépare les villes par des virgules.

Si aucune navette n'est clairement mentionnée :

- shuttle_available = false
- shuttle_cities = ""

RÈGLES GÉNÉRALES :

- N'invente jamais une information.
- Si le niveau d'hébreu est absent, écris "Non précisé".
- Si l'expérience est absente, écris "Non précisée".
- Le titre doit être court et professionnel.
- N'écris jamais le nom de l'entreprise, même dans la description.
"""

    try:
        response = client.responses.parse(
            model=os.environ.get(
                "OPENAI_MODEL",
                "gpt-4o-2024-08-06",
            ),
            input=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": cleaned_raw_job,
                },
            ],
            text_format=ExtractedJob,
        )

    except APIConnectionError as error:
        cause = repr(error.__cause__) if error.__cause__ else "cause inconnue"
        raise RuntimeError(
            "Connexion OpenAI impossible depuis Python. "
            f"Détail technique : {cause}"
        ) from error

    finally:
        client.close()

    result = response.output_parsed

    if result is None:
        raise RuntimeError(
            "Aucun résultat n'a été retourné par l'analyse."
        )

    # Toujours récupérer la référence depuis le texte original.
    detected_reference = extract_reference(raw_job)

    if detected_reference:
        result.reference = detected_reference

    # Nettoyage final de tous les champs.
    result.title = sanitize_public_text(result.title)
    result.city = sanitize_public_text(result.city)
    result.contract_type = sanitize_public_text(
        result.contract_type
    )
    result.schedule = sanitize_public_text(result.schedule)
    result.hebrew_level = sanitize_public_text(
        result.hebrew_level
    )
    result.experience = sanitize_public_text(
        result.experience
    )
    result.description = sanitize_public_text(
        result.description
    )
    result.requirements = sanitize_public_text(
        result.requirements
    )
    result.advantages = sanitize_public_text(
        result.advantages
    )

    result.shuttle_cities = sanitize_public_text(
        result.shuttle_cities
    )

    if not result.shuttle_available:
        result.shuttle_cities = ""

    if result.shuttle_available and not result.shuttle_cities:
        result.shuttle_available = False

    # Vérification ultime.
    validate_no_forbidden_content(result)

    if not result.title:
        raise ValueError("Le titre est vide après nettoyage.")

    if not result.city:
        result.city = ""

    if not result.description:
        raise ValueError(
            "La description est vide après nettoyage."
        )

    return result


def analyse_multiple_jobs(
    raw_blocks: list[str],
) -> tuple[list[ExtractedJob], list[str]]:
    """
    Analyse les offres une par une.

    C'est plus lent que le traitement parallèle,
    mais beaucoup plus stable pour 10 à 50 annonces.
    """

    extracted_jobs = []
    errors = []

    total = len(raw_blocks)

    for position, raw_block in enumerate(
        raw_blocks,
        start=1,
    ):
        reference = extract_reference(raw_block) or "inconnue"

        print(
            f"Analyse {position}/{total} "
            f"— poste {reference}"
        )

        try:
            extracted_job = analyse_raw_job(raw_block)

            extracted_jobs.append(extracted_job)

            print(
                f"Poste {reference} analysé avec succès."
            )

        except Exception as error:
            error_message = (
                f"Poste {reference} : "
                f"{type(error).__name__} — {error}"
            )

            errors.append(error_message)
            print(error_message)

    extracted_jobs.sort(
        key=lambda job: (
            int(job.reference)
            if job.reference.isdigit()
            else 0
        ),
        reverse=True,
    )

    return extracted_jobs, errors


# ============================================================
# ROUTES PUBLIQUES
# ============================================================

@app.route("/")
def index():
    search = request.args.get("search", "").strip()
    city = request.args.get("city", "").strip()
    contract = request.args.get("contract", "").strip()
    page = request.args.get("page", 1, type=int)

    query = Job.query.filter(Job.published.is_(True))

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            db.or_(
                Job.title.ilike(search_pattern),
                Job.description.ilike(search_pattern),
                Job.requirements.ilike(search_pattern),
                Job.city.ilike(search_pattern),
            )
        )

    if city:
        city_pattern = f"%{city}%"
        query = query.filter(
            db.or_(
                Job.city == city,
                db.and_(
                    Job.shuttle_available.is_(True),
                    Job.shuttle_cities.ilike(city_pattern),
                ),
            )
        )

    if contract:
        query = query.filter(Job.contract_type == contract)

    pagination = (
        query
        .order_by(Job.created_at.desc())
        .paginate(
            page=page,
            per_page=12,
            error_out=False,
        )
    )
    jobs = pagination.items

    advertisements = (
        Advertisement.query
        .filter_by(published=True)
        .order_by(
            Advertisement.display_order.asc(),
            Advertisement.created_at.desc(),
        )
        .all()
    )

    cities = [
        row[0]
        for row in (
            db.session.query(Job.city)
            .filter(
                Job.published.is_(True),
                Job.city.isnot(None),
                Job.city != "",
                Job.city != "Non précisée",
                Job.city != "Non spécifiée",
                Job.city != "À définir",
                Job.city != "Inconnue",
                Job.city != "-",
                Job.city != "N/A",
            )
            .distinct()
            .order_by(Job.city)
            .all()
        )
    ]

    shuttle_city_rows = (
        db.session.query(Job.shuttle_cities)
        .filter(
            Job.published.is_(True),
            Job.shuttle_available.is_(True),
            Job.shuttle_cities.isnot(None),
            Job.shuttle_cities != "",
        )
        .all()
    )

    all_cities = set(cities)
    for row in shuttle_city_rows:
        for shuttle_city in row[0].split(","):
            cleaned_city = shuttle_city.strip()
            if cleaned_city:
                all_cities.add(cleaned_city)

    cities = sorted(all_cities, key=lambda value: value.casefold())

    contracts = [
        row[0]
        for row in (
            db.session.query(Job.contract_type)
            .filter(
                Job.published.is_(True),
                Job.contract_type.isnot(None),
                Job.contract_type != "",
            )
            .distinct()
            .order_by(Job.contract_type)
            .all()
        )
    ]

    return render_template(
        "index.html",
        jobs=jobs,
        advertisements=advertisements,
        cities=cities,
        contracts=contracts,
        selected_search=search,
        selected_city=city,
        selected_contract=contract,
        search=search,
        city=city,
        contract=contract,
        pagination=pagination,
    )


# ============================================================
# CONNEXION ADMINISTRATEUR
# ============================================================
@app.route("/offre/<int:job_id>")
def job_detail(job_id):
    job = (
        Job.query
        .filter_by(
            id=job_id,
            published=True,
        )
        .first_or_404()
    )

    similar_jobs = (
        Job.query
        .filter(
            Job.published.is_(True),
            Job.id != job.id,
            Job.city == job.city,
        )
        .order_by(Job.created_at.desc())
        .limit(3)
        .all()
    )

    return render_template(
        "job_detail.html",
        job=job,
        similar_jobs=similar_jobs,
    )
@app.route(
    "/login",
    methods=["GET", "POST"],
)
def login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin"))

    if request.method == "POST":
        username = request.form.get(
            "username",
            "",
        ).strip()

        password = request.form.get(
            "password",
            "",
        )

        if (
            username == ADMIN_USERNAME
            and password == ADMIN_PASSWORD
        ):
            session["admin_logged_in"] = True

            flash(
                "Connexion réussie.",
                "success",
            )

            return redirect(url_for("admin"))

        flash(
            "Nom d'utilisateur ou mot de passe incorrect.",
            "error",
        )

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()

    flash(
        "Vous êtes déconnecté.",
        "success",
    )

    return redirect(url_for("login"))


# ============================================================
# ADMINISTRATION
# ============================================================

@app.route("/admin")
@admin_required
def admin():
    jobs = (
        Job.query
        .order_by(Job.created_at.desc())
        .all()
    )

    advertisements = (
        Advertisement.query
        .order_by(
            Advertisement.display_order.asc(),
            Advertisement.created_at.desc(),
        )
        .all()
    )

    return render_template(
        "admin.html",
        jobs=jobs,
        advertisements=advertisements,
    )


# ============================================================
# CRÉATION MANUELLE D'UNE OFFRE
# ============================================================
@app.route(
    "/admin/offres/nouvelle",
    methods=["GET", "POST"],
)
@admin_required
def create_job():
    if request.method == "POST":
        reference = request.form.get(
            "reference",
            "",
        ).strip()

        title = request.form.get(
            "title",
            "",
        ).strip()

        city = request.form.get(
            "city",
            "",
        ).strip()

        description = request.form.get(
            "description",
            "",
        ).strip()

        form_link = request.form.get(
            "form_link",
            "",
        ).strip()

        if (
            not reference
            or not title
            or not city
            or not description
            or not form_link
        ):
            flash(
                (
                    "Le numéro, le titre, la ville, la mission "
                    "et le lien du formulaire sont obligatoires."
                ),
                "error",
            )

            return render_template(
                "job_form.html",
                job=None,
            )

        existing_job = Job.query.filter_by(
            reference=reference
        ).first()

        if existing_job:
            flash(
                "Ce numéro de poste existe déjà.",
                "error",
            )

            return render_template(
                "job_form.html",
                job=None,
            )

        job = Job(
            reference=reference,
            title=sanitize_public_text(title),
            city=sanitize_public_text(city),
            contract_type=sanitize_public_text(
                request.form.get(
                    "contract_type",
                    "",
                )
            ),
            schedule=sanitize_public_text(
                request.form.get(
                    "schedule",
                    "",
                )
            ),
            hebrew_level=sanitize_public_text(
                request.form.get(
                    "hebrew_level",
                    "",
                )
            ),
            experience=sanitize_public_text(
                request.form.get(
                    "experience",
                    "",
                )
            ),
            description=sanitize_public_text(
                description
            ),
            requirements=sanitize_public_text(
                request.form.get(
                    "requirements",
                    "",
                )
            ),
            advantages=sanitize_public_text(
                request.form.get(
                    "advantages",
                    "",
                )
            ),
            shuttle_available=(
                request.form.get("shuttle_available") == "on"
            ),
            shuttle_cities=sanitize_public_text(
                request.form.get(
                    "shuttle_cities",
                    "",
                )
            ),
            form_link=form_link,
            published=(
                request.form.get("published") == "on"
            ),
        )

        try:
            db.session.add(job)
            db.session.commit()

        except Exception as error:
            db.session.rollback()

            flash(
                f"Erreur lors de la création : {error}",
                "error",
            )

            return render_template(
                "job_form.html",
                job=None,
            )

        flash(
            "L'offre a été créée.",
            "success",
        )

        return redirect(url_for("admin"))

    return render_template(
        "job_form.html",
        job=None,
    )

@app.route(
    "/admin/offres/<int:job_id>/modifier",
    methods=["GET", "POST"],
)
@admin_required
def edit_job(job_id):
    job = Job.query.get_or_404(job_id)

    if request.method == "POST":
        reference = request.form.get(
            "reference",
            "",
        ).strip()

        duplicate = (
            Job.query
            .filter(
                Job.reference == reference,
                Job.id != job.id,
            )
            .first()
        )

        if duplicate:
            flash(
                "Ce numéro de poste existe déjà.",
                "error",
            )

            return render_template(
                "job_form.html",
                job=job,
            )

        job.reference = reference

        job.title = sanitize_public_text(
            request.form.get(
                "title",
                "",
            )
        )

        job.city = sanitize_public_text(
            request.form.get(
                "city",
                "",
            )
        )

        job.contract_type = sanitize_public_text(
            request.form.get(
                "contract_type",
                "",
            )
        )

        job.schedule = sanitize_public_text(
            request.form.get(
                "schedule",
                "",
            )
        )

        job.hebrew_level = sanitize_public_text(
            request.form.get(
                "hebrew_level",
                "",
            )
        )

        job.experience = sanitize_public_text(
            request.form.get(
                "experience",
                "",
            )
        )

        job.description = sanitize_public_text(
            request.form.get(
                "description",
                "",
            )
        )

        job.requirements = sanitize_public_text(
            request.form.get(
                "requirements",
                "",
            )
        )

        job.advantages = sanitize_public_text(
            request.form.get(
                "advantages",
                "",
            )
        )

        job.shuttle_available = (
            request.form.get("shuttle_available") == "on"
        )

        job.shuttle_cities = sanitize_public_text(
            request.form.get(
                "shuttle_cities",
                "",
            )
        )

        if not job.shuttle_available:
            job.shuttle_cities = ""

        job.form_link = request.form.get(
            "form_link",
            "",
        ).strip()

        job.published = (
            request.form.get("published") == "on"
        )

        db.session.commit()

        flash(
            "L'offre a été modifiée.",
            "success",
        )

        return redirect(url_for("admin"))

    return render_template(
        "job_form.html",
        job=job,
    )


# ============================================================
# PUBLIER OU MASQUER UNE OFFRE
# ============================================================

@app.post(
    "/admin/offres/<int:job_id>/publier"
)
@admin_required
def toggle_job(job_id):
    job = Job.query.get_or_404(job_id)

    job.published = not job.published

    db.session.commit()

    if job.published:
        message = "L'offre est maintenant publiée."
    else:
        message = "L'offre est maintenant masquée."

    flash(
        message,
        "success",
    )

    return redirect(url_for("admin"))


# ============================================================
# SUPPRIMER UNE OFFRE
# ============================================================

@app.post(
    "/admin/offres/<int:job_id>/supprimer"
)
@admin_required
def delete_job(job_id):
    job = Job.query.get_or_404(job_id)

    db.session.delete(job)
    db.session.commit()

    flash(
        "L'offre a été supprimée.",
        "success",
    )

    return redirect(url_for("admin"))


# ============================================================
# GESTION DES PUBLICITÉS / PROFESSIONNELS
# ============================================================

def fill_advertisement_from_form(advertisement):
    advertisement.company_name = request.form.get(
        "company_name", ""
    ).strip()
    advertisement.professional_name = request.form.get(
        "professional_name", ""
    ).strip()
    advertisement.activity = request.form.get(
        "activity", ""
    ).strip()
    advertisement.specialty = request.form.get(
        "specialty", ""
    ).strip()
    advertisement.address = request.form.get(
        "address", ""
    ).strip()
    advertisement.phone = request.form.get(
        "phone", ""
    ).strip()
    advertisement.email = request.form.get(
        "email", ""
    ).strip()
    advertisement.website = request.form.get(
        "website", ""
    ).strip()
    advertisement.description = request.form.get(
        "description", ""
    ).strip()
    advertisement.icon = request.form.get(
        "icon", "💼"
    ).strip() or "💼"
    advertisement.published = (
        request.form.get("published") == "on"
    )

    try:
        advertisement.display_order = int(
            request.form.get("display_order", "0") or 0
        )
    except ValueError:
        advertisement.display_order = 0


@app.route(
    "/admin/publicites/nouvelle",
    methods=["GET", "POST"],
)
@admin_required
def create_advertisement():
    advertisement = Advertisement()

    if request.method == "POST":
        fill_advertisement_from_form(advertisement)

        if (
            not advertisement.company_name
            or not advertisement.professional_name
            or not advertisement.activity
        ):
            flash(
                "La société, le nom du professionnel et l'activité sont obligatoires.",
                "error",
            )
            return render_template(
                "advertisement_form.html",
                advertisement=advertisement,
            )

        try:
            db.session.add(advertisement)
            db.session.commit()
        except Exception as error:
            db.session.rollback()
            flash(
                f"Erreur lors de l'ajout de la publicité : {error}",
                "error",
            )
            return render_template(
                "advertisement_form.html",
                advertisement=advertisement,
            )

        flash("La publicité a été ajoutée.", "success")
        return redirect(url_for("admin"))

    return render_template(
        "advertisement_form.html",
        advertisement=None,
    )


@app.route(
    "/admin/publicites/<int:advertisement_id>/modifier",
    methods=["GET", "POST"],
)
@admin_required
def edit_advertisement(advertisement_id):
    advertisement = Advertisement.query.get_or_404(
        advertisement_id
    )

    if request.method == "POST":
        fill_advertisement_from_form(advertisement)

        if (
            not advertisement.company_name
            or not advertisement.professional_name
            or not advertisement.activity
        ):
            flash(
                "La société, le nom du professionnel et l'activité sont obligatoires.",
                "error",
            )
            return render_template(
                "advertisement_form.html",
                advertisement=advertisement,
            )

        try:
            db.session.commit()
        except Exception as error:
            db.session.rollback()
            flash(
                f"Erreur lors de la modification : {error}",
                "error",
            )
            return render_template(
                "advertisement_form.html",
                advertisement=advertisement,
            )

        flash("La publicité a été modifiée.", "success")
        return redirect(url_for("admin"))

    return render_template(
        "advertisement_form.html",
        advertisement=advertisement,
    )


@app.post(
    "/admin/publicites/<int:advertisement_id>/publier"
)
@admin_required
def toggle_advertisement(advertisement_id):
    advertisement = Advertisement.query.get_or_404(
        advertisement_id
    )
    advertisement.published = not advertisement.published
    db.session.commit()

    flash(
        "La publicité est maintenant publiée."
        if advertisement.published
        else "La publicité est maintenant masquée.",
        "success",
    )
    return redirect(url_for("admin"))


@app.post(
    "/admin/publicites/<int:advertisement_id>/supprimer"
)
@admin_required
def delete_advertisement(advertisement_id):
    advertisement = Advertisement.query.get_or_404(
        advertisement_id
    )
    db.session.delete(advertisement)
    db.session.commit()
    flash("La publicité a été supprimée.", "success")
    return redirect(url_for("admin"))


# ============================================================
# IMPORT INTELLIGENT DE PLUSIEURS OFFRES
# ============================================================

@app.route(
    "/admin/import",
    methods=["GET", "POST"],
)
@admin_required
def import_jobs():
    if request.method == "GET":
        return render_template(
            "import_jobs.html"
        )

    raw_text = request.form.get(
        "bulk_text",
        "",
    ).strip()

    form_link = request.form.get(
        "form_link",
        "",
    ).strip()

    published = (
        request.form.get("published") == "on"
    )
    print("PUBLISHED =", published)
    if not raw_text:
        flash(
            "Collez au moins une annonce.",
            "error",
        )

        return render_template(
            "import_jobs.html"
        )

    if not form_link:
        flash(
            (
                "Le lien du formulaire "
                "de candidature est obligatoire."
            ),
            "error",
        )

        return render_template(
            "import_jobs.html"
        )

    raw_blocks = split_raw_jobs(raw_text)

    if not raw_blocks:
        flash(
            (
                "Aucune annonce détectée. "
                "Chaque offre doit contenir "
                "משרה מספר suivi du numéro."
            ),
            "error",
        )

        return render_template(
            "import_jobs.html"
        )

    BATCH_SIZE = 10

    extracted_jobs = []
    analysis_errors = []

    for i in range(0, len(raw_blocks), BATCH_SIZE):

        batch = raw_blocks[i:i + BATCH_SIZE]

        try:
            jobs, errors = analyse_multiple_jobs(batch)

            extracted_jobs.extend(jobs)
            analysis_errors.extend(errors)

        except Exception as e:
            analysis_errors.append(str(e))

    imported_count = 0
    duplicate_count = 0
    database_errors = []

    for extracted in extracted_jobs:
        existing_job = Job.query.filter_by(
            reference=extracted.reference
        ).first()

        if existing_job:
            duplicate_count += 1
            continue

        try:
            # Dernier nettoyage avant la base de données.
            clean_advantages = sanitize_public_text(
                extracted.advantages
            )

            if contains_forbidden_content(
                clean_advantages
            ):
                raise ValueError(
                    "Commission interdite détectée."
                )

            job = Job(
                reference=extracted.reference,
                title=sanitize_public_text(
                    extracted.title
                ),
                city=sanitize_public_text(
                    extracted.city
                ),
                contract_type=sanitize_public_text(
                    extracted.contract_type
                ),
                schedule=sanitize_public_text(
                    extracted.schedule
                ),
                hebrew_level=sanitize_public_text(
                    extracted.hebrew_level
                ),
                experience=sanitize_public_text(
                    extracted.experience
                ),
                description=sanitize_public_text(
                    extracted.description
                ),
                requirements=sanitize_public_text(
                    extracted.requirements
                ),
                advantages=clean_advantages,
                shuttle_available=extracted.shuttle_available,
                shuttle_cities=sanitize_public_text(
                    extracted.shuttle_cities
                ),
                form_link=form_link,
                published=published,
            )

            db.session.add(job)
            db.session.commit()
            imported_count += 1

        except Exception as error:
            db.session.rollback()

            database_errors.append(
                f"Poste {extracted.reference} : "
                f"{type(error).__name__} — {error}"
            )
    total_errors = (
        analysis_errors
        + database_errors
    )

    flash(
        (
            f"{imported_count} offre(s) importée(s). "
            f"{duplicate_count} doublon(s) ignoré(s). "
            f"{len(total_errors)} erreur(s)."
        ),
        "success",
    )

    return render_template(
        "import_result.html",
        imported_count=imported_count,
        duplicate_count=duplicate_count,
        detected_count=len(raw_blocks),
        errors=total_errors,
    )


# ============================================================
# OFFRE DE DÉMONSTRATION
# ============================================================

@app.cli.command("demo")
def demo():
    existing_job = Job.query.filter_by(
        reference="29319"
    ).first()

    if existing_job:
        print("L'offre d'exemple existe déjà.")
        return

    job = Job(
        reference="29319",
        title="Responsable de site",
        city="Haïfa",
        contract_type="Temps plein",
        schedule="Dimanche à jeudi",
        hebrew_level="Bon niveau requis",
        experience="Expérience en gestion souhaitée",
        description=(
            "Gérer le site et organiser le travail "
            "de l'équipe."
        ),
        requirements=(
            "Sens des responsabilités\n"
            "Disponibilité\n"
            "Capacité à encadrer une équipe"
        ),
        advantages=(
            "Poste stable\n"
            "Environnement dynamique"
        ),
        form_link="https://forms.google.com/",
        published=True,
    )

    db.session.add(job)
    db.session.commit()

    print("Offre d'exemple créée.")


# ============================================================
# CRÉATION AUTOMATIQUE DES TABLES
# ============================================================

with app.app_context():
    db.create_all()

    inspector = inspect(db.engine)

    if "job" in inspector.get_table_names():
        existing_columns = {
            column["name"]
            for column in inspector.get_columns("job")
        }

        if "shuttle_available" not in existing_columns:
            db.session.execute(
                text(
                    "ALTER TABLE job "
                    "ADD COLUMN shuttle_available BOOLEAN "
                    "NOT NULL DEFAULT FALSE"
                )
            )

        if "shuttle_cities" not in existing_columns:
            db.session.execute(
                text(
                    "ALTER TABLE job "
                    "ADD COLUMN shuttle_cities VARCHAR(500) "
                    "DEFAULT ''"
                )
            )

        db.session.commit()


# ============================================================
# LANCEMENT LOCAL
# ============================================================

if __name__ == "__main__":
    app.run(debug=True)
