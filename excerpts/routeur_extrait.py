"""
routeur_extrait.py — Extrait simplifié du routeur de Jarvis (le module complet, routeur.py, est privé).

Le routeur décide, pour chaque demande, quel modèle d'IA y répond :
- toujours Claude (abonnement) : actions sur le Mac (appels d'outils), code, raisonnement complexe, et tout ce
  qui contient des données personnelles — ces données ne partent JAMAIS vers les API gratuites ;
- un pool de modèles gratuits (Groq, Gemini, Mistral, OpenRouter, Cerebras) d'abord pour ce qui est simple et
  public : conversation, culture générale, traductions, reformulations, résumés de pages publiques. Le pool ne
  reçoit que la question du moment, sans mémoire, historique ni état de Jarvis.

Solutions de repli (dans la version complète) : quota atteint ou erreur → fournisseur suivant ; HTTP 429 → pause
jusqu'à la réinitialisation ; réponse gratuite insuffisante → relance avec Claude ; limite Claude atteinte →
« mode relais » (actions mises en attente, données personnelles traitées par le modèle local de l'appareil).
"""

import re

TRANSLATE = re.compile(r"\b(?:tradui[st]|traduction|translate|comment (?:dit-on|on dit)|en (?:anglais|espagnol|allemand|"
                       r"italien|portugais|chinois|japonais|arabe)\b)", re.I)
REFORMULATE = re.compile(r"\b(?:reformule|reformulation|paraphrase|réécri[st]|rends?(?:-le)? plus (?:clair|poli|formel|court)|"
                         r"corrige (?:l'orthographe|les fautes)|simplifie (?:ce|cette|le) (?:texte|phrase))\b", re.I)
SUMMARY = re.compile(r"\b(?:résume|résumé|synthèse|en bref|l'essentiel)\b", re.I)
PUBLIC_URL = re.compile(r"https?://\S+", re.I)
GENERAL = re.compile(r"^(?:qu'est-ce que|qu'est ce que|c'est quoi|qui (?:est|était|a)|quel(?:le)?s? (?:est|sont|était)|"
                     r"pourquoi|comment (?:fonctionne|marche|se fait)|combien|quand (?:a|est|était)|où (?:est|se trouve)|"
                     r"explique|définis|définition|que veut dire|que signifie|donne-moi (?:une|des) idées?)\b", re.I)
CHAT = re.compile(r"^(?:salut|bonjour|bonsoir|coucou|hello|merci|ça va|comment vas-tu|comment allez-vous|"
                  r"raconte(?:-moi)? une blague|tu (?:connais|aimes)|quel temps)\b", re.I)
COMPLEX = re.compile(r"\b(?:analyse|compare|stratégie|démontre|prouve|étape par étape|raisonne|planifie|plan détaillé|"
                     r"optimise|conçois|architecture|diagnostique|calcule|équation|intégrale|dérivée|probabilit)\w*", re.I)
CODE = re.compile(r"```|\b(?:code|fonction|script|python|javascript|bug|erreur de compilation|regex|sql|api|"
                  r"classe|algorithme|programme|git|terminal)\b", re.I)
# Données personnelles : jamais vers le pool gratuit
PERSONAL = re.compile(r"\b(?:mon|ma|mes) (?:cv|candidatures?|lettres? de motivation|e-?mails?|courriels?|mails?|notes|"
                      r"fichiers?|documents?|captures?|écran|mémoire|adresse|numéro|téléphone|mot de passe|compte|"
                      r"banque|salaire|santé|dossier|rapport|échéances?|agenda|calendrier|contacts?|messages?)\b|"
                      r"\b(?:candidature|recruteur|entretien d'embauche)\b|[\w.+-]+@[\w-]+\.[\w.]+|"
                      r"(?:\+?\d[\d .-]{8,}\d)", re.I)
# La question dépend de la conversation (« et lui ? », « traduis ça ») : le pool ne voit pas l'historique
CONTEXTUAL = re.compile(r"^(?:et |mais |alors |donc |aussi )|\b(?:ça|cela|ceci|celui-ci|celle-ci|le précédent|"
                        r"la précédente|ci-dessus|plus haut|ta réponse|ton message|le même|la même|lui|elle|eux)\b", re.I)
REFUSAL = re.compile(r"\b(?:je ne peux pas|je ne suis pas en mesure|i can(?:no|')t|i'm unable|as an ai|en tant qu'ia, je ne|"
                     r"je n'ai pas accès|je ne sais pas|désolé, je ne)\b", re.I)


class Decision:
    def __init__(self, category: str, claude_only: bool, reason: str, free_first: bool = False,
                 cacheable: bool = False, local_ok: bool = True):
        self.category = category
        self.claude_only = claude_only
        self.reason = reason
        self.free_first = free_first
        self.cacheable = cacheable
        self.local_ok = local_ok              # mode relais : le modèle local peut-il la traiter ? (pas les actions)

    def __repr__(self):
        return f"Decision({self.category}, claude_only={self.claude_only}, free_first={self.free_first})"


def classify(text: str, need: str | None = None, sensitive: bool = False, action: bool = False,
             attachments: bool = False, mode: str = "equilibre") -> Decision:
    """Où envoyer la demande ? Claude obligatoire, ou le pool gratuit d'abord (selon le mode)."""
    t = (text or "").strip()
    if action:
        return Decision("action", True, "appel d'outils : actions sur le Mac", local_ok=False)
    if need == "vision":
        return Decision("vision", True, "capture d'écran : données personnelles")
    if sensitive or attachments or PERSONAL.search(t):
        return Decision("personnel", True, "données personnelles : jamais vers les API gratuites")
    if need == "code" or CODE.search(t):
        return Decision("code", True, "code : Claude")
    if COMPLEX.search(t) or len(t) > 600:
        return Decision("complexe", True, "raisonnement complexe : Claude")
    contextual = bool(CONTEXTUAL.search(t))
    if TRANSLATE.search(t):
        cat = "traduction"
    elif REFORMULATE.search(t):
        cat = "reformulation"
    elif SUMMARY.search(t) and PUBLIC_URL.search(t):
        cat = "resume_web"
    elif CHAT.search(t):
        cat = "conversation"
    elif GENERAL.search(t) and len(t) < 220:
        cat = "culture"
    else:
        cat = "general"
    if contextual and cat not in ("traduction", "reformulation"):
        # Le pool ne voit pas l'historique (vie privée) : une question qui en dépend reste à Claude
        return Decision(cat, False, "dépend de la conversation : Claude", free_first=False)
    if mode == "qualite":
        free_first = False
    elif mode == "economie":
        free_first = True
    else:
        free_first = cat != "general"
    cacheable = cat in ("culture", "traduction", "reformulation", "resume_web") and not contextual
    why = {"traduction": "traduction", "reformulation": "reformulation", "resume_web": "résumé d'une page publique",
           "conversation": "conversation simple", "culture": "culture générale", "general": "demande ordinaire"}[cat]
    return Decision(cat, False, why + (" → pool gratuit d'abord" if free_first else " → Claude d'abord"),
                    free_first=free_first, cacheable=cacheable)


def insufficient(answer: str | None, question: str = "") -> str | None:
    """Pourquoi une réponse gratuite est insuffisante (None : elle convient) → relance avec Claude."""
    a = (answer or "").strip()
    if not a:
        return "réponse vide"
    if REFUSAL.search(a[:300]):
        return "refus ou ignorance"
    words = len(a.split())
    if words < 3 and len((question or "").split()) > 6:
        return "réponse trop courte"
    return None
