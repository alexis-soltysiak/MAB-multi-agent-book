# Book Agents — Pipeline multi-agents *event-driven*

> Générateur de livres basé sur une architecture multi-agents pilotée par événements (Redis Streams) avec persistance PostgreSQL et une UI d’observabilité.

## Sommaire

* [Démarrage rapide (A → Z)](#démarrage-rapide-a--z)

  * [Pré-requis](#pré-requis)
  * [Démarrer l’infra (Redis + Postgres)](#démarrer-linfra-redis--postgres)
  * [Initialiser la base de données](#initialiser-la-base-de-données)
  * [Lancer les workers (agents)](#lancer-les-workers-agents)
  * [Lancer l’UI d’observabilité](#lancer-lui-dobservabilité)
  * [Démarrer un livre](#démarrer-un-livre)
  * [Exporter le livre final](#exporter-le-livre-final)
* [Architecture & fonctionnement](#architecture--fonctionnement)

  * [Vue d’ensemble](#vue-densemble)
  * [Modèle de message (Envelope)](#modèle-de-message-envelope)
  * [Flux complet (pipeline)](#flux-complet-pipeline)
* [Observabilité](#observabilité)

  * [Trace steps](#trace-steps)
  * [LLM calls](#llm-calls)
  * [Suivre l’avancement](#suivre-lavancement)
* [Où se trouve le “livre final” ?](#où-se-trouve-le-livre-final-)

---

## Démarrage rapide (A → Z)


### Pré-requis

* Docker + Docker Compose
* Python (venv) + dépendances installées
* Une clé OpenAI configurée (`OPENAI_API_KEY` dans `.env`)
* Redis + PostgreSQL (fournis via Docker Compose)

> Recommandation : utilisez un `.env` à la racine du projet.

Exemple (à adapter) :

```env
OPENAI_API_KEY=<CHANGE ME>
OPENAI_MODEL=gpt-4o-mini <CHANGE ME IF YOU WANT>
STREAM_KEY=book_agents:events
DATABASE_URL=postgresql+asyncpg://book:book@localhost:5432/book_agents
REDIS_URL=redis://localhost:6379/0
LOG_LEVEL=INFO
```

---


### Démarrage Python (environnement local)

Avant de démarrer l’infrastructure Docker, assure-toi d’être dans le bon dossier et d’avoir un environnement Python isolé.

#### 1) Se placer dans le dossier du projet

```bash
cd path/to/your/project
```

#### 2) Créer un environnement virtuel

```bash
python -m venv .venv
```

#### 3) Activer le venv

**Windows (PowerShell)**

```bash
.venv\Scripts\Activate.ps1
```

**macOS / Linux**

```bash
source .venv/bin/activate
```

#### 4) Installer le projet et ses dépendances

Ce projet utilise une configuration **PEP 621** (`pyproject.toml`). Il n’y a donc **pas de requirement.txt**

Installation recommandée (mode développement) :

```bash
pip install -e .
```

Cela permet :

* d’installer toutes les dépendances déclarées dans `pyproject.toml`
* de rendre la commande `book-agents` disponible dans le shell
* de refléter immédiatement les changements de code sans réinstallation

> Vérifie que ton fichier `.env` est bien présent avant de continuer.




### Démarrage Docker / Db / redis 

allez dans votre projet 

### Démarrer l’infra (Redis + Postgres)

Réinitialisation complète (volumes inclus) :

```bash
docker compose down -v
docker compose up -d
```

### Initialiser la base de données

```bash
book-agents init-db

```
---

### Lancer les workers (agents)

Chaque agent est un **worker** qui :

1. écoute le bus **Redis Streams**,
2. consomme des événements,
3. exécute sa logique,
4. republie de nouveaux événements.

#### Option A — Lancer les agents un par un

Ouvrez **6 terminaux** et lancez :

```bash
book-agents run-worker coordinator
book-agents run-worker planner
book-agents run-worker writer
book-agents run-worker continuity
book-agents run-worker editor
book-agents run-worker bible
```

#### Option B — Lancer tous les agents d’un coup

```bash
book-agents run-all
```

### Lancer l’UI d’observabilité

L’UI permet de suivre :

* le pipeline (qui est actif / dernier step),
* le flux chronologique des événements,
* les calls LLM (`running/ok/error`), prompts, outputs, latences.

**Dans un autre terminal :**
```bash
book-agents ui
```

### Démarrer un livre

Vous fournissez un **spec JSON** (ex. `title`, `genre`, `premise`, `n_chapters`, etc.).
Cela publie l’événement initial sur le bus.

**Dans un 3ième terminal :**

```bash
book-agents start-book spec.example.json
```

La commande affiche un `book_id` **à conserver**.

**important** : Vous pouvez actualiser votre UI avec F5 : http://127.0.0.1:7860/


### Exporter le livre final

Quand les chapitres sont finalisés, export en Markdown :
( vous pouvez aussi l'exporter de l'ui maintenant )
```bash
book-agents export-book <BOOK_ID> --out book.md
```

---

## Architecture & fonctionnement

### Vue d’ensemble

Le projet est un pipeline multi-agents **piloté par événements** (*event-driven*).
Les agents ne s’appellent **jamais directement** : ils communiquent uniquement via le bus.

* **Bus** : Redis Streams (`book_agents:events`)
* **État** : PostgreSQL (`books`, `chapters`, `traces`, `llm_calls`)
* **Observabilité** : `trace_steps` + `llm_calls` (visualisés dans l’UI)

#### Schéma (haut niveau)

```mermaid
flowchart LR
  Start[book.start.requested] --> C[CoordinatorAgent]
  C --> P[PlannerAgent]
  P --> C
  C --> W[WriterAgent]
  W --> K[ContinuityAgent]
  K -->|approved| E[EditorAgent]
  K -->|needs rewrite| W
  E --> C
  E --> B[BibleAgent]
  B --> C
  C --> Done[book.completed]
```

### Modèle de message (Envelope)

Chaque message est un **Envelope** :

* `event_type` : type d’événement (ex. `chapter.draft.created`)
* `book_id` : identifiant du livre
* `payload` : données utiles
* `meta` : informations de contrôle (`attempt`, `trace_step_id`, etc.)

---

## Flux complet (pipeline)

### 0) Événement initial

Lorsque vous lancez `start-book`, le système publie :

* `book.start.requested` *(payload = spec)*

### 1) CoordinatorAgent (orchestrateur)

**Rôle** : créer l’état minimal, puis piloter la suite.

* Reçoit `book.start.requested`

  * crée le livre (si nécessaire)
  * crée les chapitres “vides” en DB (1..n)
  * publie `book.created`

* Reçoit `book.outline.created`

  * calcule le prochain chapitre non finalisé
  * publie `chapter.write.requested` *(chapter_number)*

* Reçoit `chapter.finalized`

  * si tous les chapitres sont finalisés → publie `book.completed`
  * sinon → publie `chapter.write.requested` pour le chapitre suivant

### 2) PlannerAgent (outline)

**Rôle** : générer l’outline complet + les plans de chapitres.

* Reçoit `book.created`

  * appelle le LLM (*structured output*)
  * stocke `book.outline` en DB
  * stocke `chapter.plan` pour chaque chapitre
  * publie `book.outline.created`

### 3) WriterAgent (draft)

**Rôle** : écrire un draft complet pour un chapitre.

* Reçoit `chapter.write.requested` (ou `chapter.rewrite.requested`)

  * récupère outline + plan + bible + résumés précédents
  * appelle le LLM → `draft.text` + `draft.summary`
  * stocke en DB
  * publie `chapter.draft.created`

### 4) ContinuityAgent (review)

**Rôle** : valider la cohérence / continuité du draft.

* Reçoit `chapter.draft.created`

  * appelle le LLM en mode review
  * si `approved=true` → publie `chapter.edit.requested` *(instructions vides)*
  * sinon :

    * si `max_rewrites_per_chapter` non dépassé → publie `chapter.rewrite.requested`
    * sinon → publie `error.raised` **+** `chapter.edit.requested` (édition forcée avec instructions)

### 5) EditorAgent (finalisation)

**Rôle** : améliorer le style sans casser la continuité.

* Reçoit `chapter.edit.requested`

  * récupère draft + bible + plan
  * appelle le LLM → `edited.text`
  * stocke en DB *(status=final)*
  * publie `chapter.finalized`

### 6) BibleAgent (story bible)

**Rôle** : mettre à jour la bible après chaque chapitre finalisé.

* Reçoit `chapter.finalized`

  * appelle le LLM pour mettre à jour la story bible (normalisation / dédup)
  * stocke en DB (`book.bible`)
  * publie `story.bible.updated`

---

## Observabilité

### Trace steps

Chaque worker, lorsqu’il consomme un événement, crée un **TraceStep** :

* event entrant (type + extrait payload)
* agent qui traite
* statut (`started` → `ok` / `error`)
* events sortants (extraits)
* timestamps (`created_at`, `finished_at`)

### LLM calls

Chaque appel LLM est persisté dans `llm_calls` :

* `status` : `running` puis `ok/error`
* `latency_ms`
* `request` : `system` + `user`
* `output_parsed` + `output_text` (si présent)
* `error` (si échec)

L’UI regroupe tout et permet de filtrer par :

* agent
* type (`STEP` / `LLM`)
* status
* recherche texte

### Suivre l’avancement

Deux sources fiables :

1. **UI** : barre de progression (finalisés / total) + stream d’événements
2. **DB** : `count_final_chapters(book_id)` vs `spec.n_chapters`

---

## Où se trouve le “livre final” ?

Le contenu final est stocké dans PostgreSQL :

* `chapters.edited_text` (ou `draft_text` si besoin)
* `chapters.status = final`

Récupération via :

```bash
book-agents export-book <BOOK_ID> --out book.md
```

**OU** 

Via le bouton Export dans l'UI












