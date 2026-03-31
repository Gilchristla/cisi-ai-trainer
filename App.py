import json
import os
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Any, Tuple, Optional

import streamlit as st
from openai import OpenAI
from supabase import create_client
def get_supabase():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)


def get_email_redirect_url() -> Optional[str]:
    """Resolve the post-verification redirect URL used by Supabase email links."""
    candidates = [
        st.secrets.get("EMAIL_REDIRECT_URL"),
        st.secrets.get("APP_URL"),
        os.getenv("EMAIL_REDIRECT_URL"),
        os.getenv("APP_URL"),
    ]
    for candidate in candidates:
        if candidate:
            return candidate.rstrip("/")
    return None

# Optional live countdown support.
# The app still works without this package.
try:
    from streamlit_autorefresh import st_autorefresh
    AUTOREFRESH_AVAILABLE = True
except Exception:
    AUTOREFRESH_AVAILABLE = False

# ------------------------------------------------------------
# Login
# ------------------------------------------------------------
def login_page():
    supabase = get_supabase()
    st.title("CISI AI Trainer")
    st.subheader("Sign in or create an account")

    mode = st.radio("Choose action", ["Login", "Sign up"], horizontal=True)

    email = st.text_input("Email")
    password = st.text_input("Password", type="password")

    display_name = ""
    if mode == "Sign up":
        display_name = st.text_input("Display name")

    if st.button(mode):
        try:
            if mode == "Sign up":
                signup_payload = {
                    "email": email,
                    "password": password,
                }

                redirect_url = get_email_redirect_url()
                if redirect_url:
                    signup_payload["options"] = {"email_redirect_to": redirect_url}

                response = supabase.auth.sign_up(signup_payload)

                user = response.user
                session = response.session

                if user:
                    ensure_profile_exists(
                        supabase=supabase,
                        user=user,
                        email=email,
                        display_name=display_name or None,
                    )

                if session:
                    st.session_state.user = user
                    st.session_state.profile = load_current_profile(supabase, user.id)
                    st.success("Account created and signed in.")
                    st.rerun()
                else:
                    st.success("Account created. If email confirmation is enabled, check your inbox before logging in.")
                    if not redirect_url:
                        st.info(
                            "Set EMAIL_REDIRECT_URL (or APP_URL) in Streamlit secrets to ensure "
                            "verification links return to a reachable page."
                        )

            else:
                response = supabase.auth.sign_in_with_password({
                    "email": email,
                    "password": password
                })

                user = response.user
                st.session_state.user = user
                st.session_state.profile = load_current_profile(supabase, user.id)

                st.success("Logged in successfully")
                st.rerun()

        except Exception as e:
            st.error(f"Authentication failed: {e}")

def init_auth_state():
    defaults = {
        "user": None,
        "profile": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def ensure_profile_exists(supabase, user, email: str, display_name: Optional[str] = None):
    payload = {
        "id": user.id,
        "email": email,
        "display_name": display_name or email.split("@")[0],
    }
    supabase.table("profiles").upsert(payload).execute()


def load_current_profile(supabase, user_id: str):
    result = (
        supabase
        .table("profiles")
        .select("*")
        .eq("id", user_id)
        .single()
        .execute()
    )
    return result.data


def sign_out_user():
    st.session_state.user = None
    st.session_state.profile = None
    st.rerun()

# ------------------------------------------------------------
# FILE PATHS
# ------------------------------------------------------------
JSON_FILE = "textbook_chunks.json"
ATTEMPTS_FILE = "question_attempts.json"
WRONG_FILE = "wrong_answers.json"
REVIEW_FILE = "review_schedule.json"

MAX_SOURCE_CHARS = 100000
DEFAULT_EXAM_DURATION_MIN = 30


# ------------------------------------------------------------
# BASIC FILE HELPERS
# ------------------------------------------------------------
def load_json_file(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def append_json_record(path: str, record: Dict[str, Any]) -> None:
    data = load_json_file(path, [])
    data.append(record)
    save_json_file(path, data)


# ------------------------------------------------------------
# PARSED TEXTBOOK LOADING / GROUPING
# ------------------------------------------------------------
def load_records(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def group_sections(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, int], Dict[str, Any]] = {}

    for r in records:
        key = (r["chapter_name"], r["section_number"])

        if key not in grouped:
            grouped[key] = {
                "chapter_name": r["chapter_name"],
                "section_number": r["section_number"],
                "section_title": r["section_title"],
                "page_start": r.get("page_start"),
                "texts": [],
            }

        grouped[key]["texts"].append((r.get("chunk_index", 0), r["text"]))

    sections = []
    for _, item in grouped.items():
        item["texts"].sort(key=lambda x: x[0])
        full_text = "\n\n".join(text for _, text in item["texts"])
        sections.append({
            "chapter_name": item["chapter_name"],
            "section_number": item["section_number"],
            "section_title": item["section_title"],
            "page_start": item["page_start"],
            "text": full_text,
        })

    sections.sort(key=lambda s: (s["chapter_name"], s["section_number"]))
    return sections


def build_chapter_map(sections: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    chapter_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for s in sections:
        chapter_map[s["chapter_name"]].append(s)

    for chapter in chapter_map:
        chapter_map[chapter].sort(key=lambda x: x["section_number"])

    return dict(chapter_map)


def section_key(chapter_name: str, section_number: int) -> str:
    return f"{chapter_name} || {section_number}"


def section_label(section: Dict[str, Any]) -> str:
    return f"{section['chapter_name']} | Section {section['section_number']} - {section['section_title']}"


def build_section_lookup(sections: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {section_label(s): s for s in sections}


# ------------------------------------------------------------
# SOURCE PREPARATION
# ------------------------------------------------------------
def build_combined_source(selected_sections: List[Dict[str, Any]]) -> Tuple[str, bool]:
    """
    Combine selected sections into one source string.
    Returns (source_text, too_large_flag)
    """
    parts = []
    total_chars = 0

    for s in selected_sections:
        block = (
            f"[CHAPTER] {s['chapter_name']}\n"
            f"[SECTION_NUMBER] {s['section_number']}\n"
            f"[SECTION_TITLE] {s['section_title']}\n"
            f"[PAGE_START] {s.get('page_start')}\n"
            f"[TEXT]\n{s['text']}\n\n"
        )

        total_chars += len(block)
        parts.append(block)

    combined = "\n".join(parts)

    if len(combined) > MAX_SOURCE_CHARS:
        return combined, True

    return combined, False


# ------------------------------------------------------------
# PROMPTING
# ------------------------------------------------------------
def build_prompt(
    source_text: str,
    num_questions: int,
    selected_sections: List[Dict[str, Any]],
    exam_mode: bool,
) -> str:
    section_summary = "\n".join(
        [
            f"- {s['chapter_name']} | Section {s['section_number']} - {s['section_title']}"
            for s in selected_sections
        ]
    )

    return f"""
You are a senior examiner writing difficult CISI-style multiple-choice questions.

Use ONLY the source material below.
Do not use outside knowledge.
Do not invent facts not supported by the source text.

SELECTED SOURCE AREAS:
{section_summary}

SOURCE TEXT:
{source_text}

TASK:
Create exactly {num_questions} multiple-choice questions.

REQUIREMENTS:
- Make them challenging and exam-style
- Use 4 options only: A, B, C, D
- Exactly one correct answer per question
- Cover a mix of concept, application, interpretation, and calculation where possible
- Do not repeat the same idea too often
- Every question must clearly belong to one of the supplied sections
- For every question, include:
  - chapter_name
  - section_number
  - section_title
  - question_number
  - question_text
  - options
  - correct_answer
  - explanation
  - source_reference
  - option_feedback

OPTION_FEEDBACK RULE:
- Provide feedback for all four options A-D
- For the correct option, explain why it is right
- For the wrong options, explain why each one is wrong
- Keep each option feedback short but clear

SOURCE_REFERENCE RULE:
- Use a short phrase or concept reference from the supplied text
- Do not quote long passages

OUTPUT FORMAT:
Return valid JSON only, with this exact structure:

{{
  "questions": [
    {{
      "chapter_name": "Chapter name here",
      "section_number": 1,
      "section_title": "Section title here",
      "question_number": 1,
      "question_text": "Question here",
      "options": {{
        "A": "Option A",
        "B": "Option B",
        "C": "Option C",
        "D": "Option D"
      }},
      "correct_answer": "B",
      "explanation": "Why B is correct",
      "source_reference": "Short phrase from the source text",
      "option_feedback": {{
        "A": "Why A is wrong or right",
        "B": "Why B is wrong or right",
        "C": "Why C is wrong or right",
        "D": "Why D is wrong or right"
      }}
    }}
  ]
}}

Return JSON only.
No markdown.
No code fences.
""".strip()


def call_openai_for_questions(prompt: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key and "OPENAI_API_KEY" in st.secrets:
        api_key = st.secrets["OPENAI_API_KEY"]

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    client = OpenAI(api_key=api_key)

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
    )

    raw_text = response.output_text.strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"Model did not return valid JSON.\n\nRaw output:\n{raw_text}")


def normalize_question_payload(
    result: Dict[str, Any],
    selected_sections: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if "questions" not in result or not isinstance(result["questions"], list):
        raise ValueError("Returned JSON does not contain a valid 'questions' list.")

    fallback_section = selected_sections[0] if len(selected_sections) == 1 else None
    normalized_questions = []

    for idx, q in enumerate(result["questions"], start=1):
        options = q.get("options", {})
        option_feedback = q.get("option_feedback", {})

        normalized = {
            "chapter_name": q.get("chapter_name") or (fallback_section["chapter_name"] if fallback_section else "Unknown"),
            "section_number": q.get("section_number") or (fallback_section["section_number"] if fallback_section else -1),
            "section_title": q.get("section_title") or (fallback_section["section_title"] if fallback_section else "Unknown"),
            "question_number": q.get("question_number", idx),
            "question_text": q.get("question_text", f"Question {idx}"),
            "options": {
                "A": options.get("A", ""),
                "B": options.get("B", ""),
                "C": options.get("C", ""),
                "D": options.get("D", ""),
            },
            "correct_answer": str(q.get("correct_answer", "A")).strip().upper(),
            "explanation": q.get("explanation", "No explanation provided."),
            "source_reference": q.get("source_reference", "No source reference provided."),
            "option_feedback": {
                "A": option_feedback.get("A", ""),
                "B": option_feedback.get("B", ""),
                "C": option_feedback.get("C", ""),
                "D": option_feedback.get("D", ""),
            },
        }

        if normalized["correct_answer"] not in {"A", "B", "C", "D"}:
            normalized["correct_answer"] = "A"

        normalized_questions.append(normalized)

    return normalized_questions


# ------------------------------------------------------------
# SESSION STATE
# ------------------------------------------------------------
def init_session_state():
    defaults = {
        "generated_questions": None,
        "submitted_answers": {},
        "show_results": False,
        "results_saved": False,
        "selected_section_keys": [],
        "selected_section_labels": [],
        "quiz_id": None,
        "quiz_generated_at": None,
        "quiz_mode": "selected",  # selected / weak
        "exam_mode": False,
        "exam_duration_min": DEFAULT_EXAM_DURATION_MIN,
        "exam_started_at": None,
        "weak_quiz_section_count": 3,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def reset_quiz_state():
    st.session_state.generated_questions = None
    st.session_state.submitted_answers = {}
    st.session_state.show_results = False
    st.session_state.results_saved = False
    st.session_state.quiz_id = None
    st.session_state.quiz_generated_at = None
    st.session_state.exam_started_at = None

    # Clear radio widget keys
    keys_to_delete = []
    for k in st.session_state.keys():
        if str(k).startswith("radio_q_"):
            keys_to_delete.append(k)

    for k in keys_to_delete:
        del st.session_state[k]


# ------------------------------------------------------------
# ATTEMPT / WRONG ANSWER / REVIEW SAVE LOGIC
# ------------------------------------------------------------
def load_review_state() -> Dict[str, Any]:
    return load_json_file(REVIEW_FILE, {})


def save_review_state(state: Dict[str, Any]) -> None:
    save_json_file(REVIEW_FILE, state)


def update_review_schedule_for_section(
    state: Dict[str, Any],
    chapter_name: str,
    section_number: int,
    section_title: str,
    was_correct: bool,
):
    """
    Simple section-level spaced repetition.
    Wrong answer -> bring topic back tomorrow.
    Correct answer -> push it out gradually.
    """
    key = section_key(chapter_name, section_number)
    now = datetime.now()

    if key not in state:
        state[key] = {
            "chapter_name": chapter_name,
            "section_number": section_number,
            "section_title": section_title,
            "stage": 0,
            "next_review_at": None,
            "last_result": None,
            "wrong_events": 0,
            "correct_events": 0,
        }

    item = state[key]
    intervals_days = [1, 3, 7, 14, 30]

    if was_correct:
        item["correct_events"] += 1
        item["stage"] = min(item["stage"] + 1, len(intervals_days) - 1)
        item["last_result"] = "correct"
        item["next_review_at"] = (now + timedelta(days=intervals_days[item["stage"]])).isoformat()
    else:
        item["wrong_events"] += 1
        item["stage"] = 0
        item["last_result"] = "wrong"
        item["next_review_at"] = (now + timedelta(days=1)).isoformat()


def persist_quiz_results(
    questions: List[Dict[str, Any]],
    submitted_answers: Dict[str, str],
    quiz_id: str,
):
    timestamp = datetime.now().isoformat()
    review_state = load_review_state()
    supabase = get_supabase()

    for i, q in enumerate(questions, start=1):
        qid = f"q_{i}"
        selected = submitted_answers.get(qid)
        correct = q["correct_answer"]
        is_correct = selected == correct

        attempt_record = {
            "user_id": st.session_state.user.id,
            "quiz_id": quiz_id,
            "timestamp": timestamp,
            "chapter_name": q.get("chapter_name"),
            "section_number": q.get("section_number"),
            "section_title": q.get("section_title"),
            "question_number": q.get("question_number"),
            "question_text": q.get("question_text"),
            "selected_answer": selected,
            "correct_answer": correct,
            "is_correct": is_correct,
        }
        supabase.table("attempts").insert(attempt_record).execute()

        if not is_correct:
            wrong_record = {
                "quiz_id": quiz_id,
                "timestamp": timestamp,
                "chapter_name": q.get("chapter_name"),
                "section_number": q.get("section_number"),
                "section_title": q.get("section_title"),
                "question_number": q.get("question_number"),
                "question_text": q.get("question_text"),
                "options": q.get("options", {}),
                "selected_answer": selected,
                "correct_answer": correct,
                "explanation": q.get("explanation"),
                "source_reference": q.get("source_reference"),
                "selected_option_feedback": q.get("option_feedback", {}).get(selected, "") if selected else "",
                "all_option_feedback": q.get("option_feedback", {}),
            }
            supabase.table("wrong_answers").insert(wrong_record).execute()

        update_review_schedule_for_section(
            state=review_state,
            chapter_name=q.get("chapter_name"),
            section_number=q.get("section_number"),
            section_title=q.get("section_title"),
            was_correct=is_correct,
        )

    save_review_state(review_state)


# ------------------------------------------------------------
# ANALYTICS / WEAK TOPICS / PERFORMANCE
# ------------------------------------------------------------
def build_attempt_stats() -> Dict[str, Dict[str, Any]]:
    attempts = load_json_file(ATTEMPTS_FILE, [])
    review_state = load_review_state()

    stats: Dict[str, Dict[str, Any]] = {}

    for attempt in attempts:
        key = section_key(attempt["chapter_name"], attempt["section_number"])
        if key not in stats:
            stats[key] = {
                "chapter_name": attempt["chapter_name"],
                "section_number": attempt["section_number"],
                "section_title": attempt.get("section_title", ""),
                "attempts": 0,
                "correct": 0,
                "wrong": 0,
                "accuracy": None,
                "last_attempt_at": None,
                "next_review_at": None,
                "due_for_review": False,
                "review_stage": None,
            }

        stats[key]["attempts"] += 1
        if attempt["is_correct"]:
            stats[key]["correct"] += 1
        else:
            stats[key]["wrong"] += 1

        stats[key]["last_attempt_at"] = attempt["timestamp"]

    now = datetime.now()

    for key, item in stats.items():
        if item["attempts"] > 0:
            item["accuracy"] = round(100 * item["correct"] / item["attempts"], 1)

        if key in review_state:
            next_review_at = review_state[key].get("next_review_at")
            item["next_review_at"] = next_review_at
            item["review_stage"] = review_state[key].get("stage", 0)

            if next_review_at:
                try:
                    due = datetime.fromisoformat(next_review_at) <= now
                except Exception:
                    due = False
            else:
                due = False

            item["due_for_review"] = due

    return stats


def build_overall_summary() -> Dict[str, Any]:
    attempts = load_json_file(ATTEMPTS_FILE, [])
    total = len(attempts)
    correct = sum(1 for a in attempts if a["is_correct"])
    wrong = total - correct
    accuracy = round(100 * correct / total, 1) if total else None

    return {
        "total_attempts": total,
        "correct": correct,
        "wrong": wrong,
        "accuracy": accuracy,
    }


def build_weak_topic_rankings(all_sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Composite ranking:
    - due_for_review gets a big boost
    - low accuracy is bad
    - more wrong answers is bad
    - no history gets low priority in weak ranking
    """
    stats = build_attempt_stats()
    now = datetime.now()

    rankings = []

    for s in all_sections:
        key = section_key(s["chapter_name"], s["section_number"])
        stat = stats.get(key, {
            "attempts": 0,
            "correct": 0,
            "wrong": 0,
            "accuracy": None,
            "due_for_review": False,
            "next_review_at": None,
            "review_stage": None,
        })

        accuracy = stat["accuracy"]
        attempts = stat["attempts"]
        wrong = stat["wrong"]
        due = stat["due_for_review"]

        score = 0.0

        if due:
            score += 50

        if attempts > 0 and accuracy is not None:
            score += max(0, 100 - accuracy)

        score += wrong * 5

        rankings.append({
            "chapter_name": s["chapter_name"],
            "section_number": s["section_number"],
            "section_title": s["section_title"],
            "attempts": attempts,
            "wrong": wrong,
            "accuracy": accuracy,
            "due_for_review": due,
            "next_review_at": stat["next_review_at"],
            "weak_score": round(score, 1),
            "section_object": s,
        })

    rankings.sort(
        key=lambda x: (
            x["weak_score"],
            x["wrong"],
            -(x["attempts"] if x["attempts"] is not None else 0),
        ),
        reverse=True,
    )
    return rankings


def select_weak_sections(all_sections: List[Dict[str, Any]], max_sections: int) -> List[Dict[str, Any]]:
    rankings = build_weak_topic_rankings(all_sections)

    meaningful = [
        r for r in rankings
        if r["due_for_review"] or r["wrong"] > 0 or (r["accuracy"] is not None and r["accuracy"] < 80)
    ]

    if not meaningful:
        return []

    return [r["section_object"] for r in meaningful[:max_sections]]


def recent_wrong_answers(limit: int = 50) -> List[Dict[str, Any]]:
    wrongs = load_json_file(WRONG_FILE, [])
    wrongs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return wrongs[:limit]


# ------------------------------------------------------------
# QUESTION RENDERING / SCORING
# ------------------------------------------------------------
def render_question_block(question: Dict[str, Any], index: int):
    qid = f"q_{index}"

    st.markdown(f"### Question {question['question_number']}")
    st.caption(
        f"{question.get('chapter_name', 'Unknown Chapter')} | "
        f"Section {question.get('section_number', 'Unknown')} - "
        f"{question.get('section_title', 'Unknown Section')}"
    )
    st.write(question["question_text"])

    options = question["options"]
    labels = ["A", "B", "C", "D"]

    current_value = st.session_state.submitted_answers.get(qid)
    selected = st.radio(
        "Choose an answer:",
        labels,
        format_func=lambda x: f"{x}. {options[x]}",
        key=f"radio_{qid}",
        index=labels.index(current_value) if current_value in labels else None,
    )
    st.session_state.submitted_answers[qid] = selected

    if st.session_state.show_results:
        correct = question["correct_answer"]
        if selected == correct:
            st.success(f"Correct: {correct}")
        else:
            st.error(f"Your answer: {selected} | Correct answer: {correct}")

            if selected:
                selected_feedback = question.get("option_feedback", {}).get(selected)
                if selected_feedback:
                    st.write(f"**Why your answer was wrong:** {selected_feedback}")

        st.write(f"**Explanation:** {question['explanation']}")
        st.write(f"**Source reference:** {question['source_reference']}")

        with st.expander("See feedback for all options"):
            for label in labels:
                feedback = question.get("option_feedback", {}).get(label, "")
                prefix = "✅" if label == correct else "❌"
                st.write(f"{prefix} **{label}**: {feedback}")


def score_answers(questions: List[Dict[str, Any]]) -> Tuple[int, int]:
    score = 0
    unanswered = 0

    for i, q in enumerate(questions, start=1):
        qid = f"q_{i}"
        selected = st.session_state.submitted_answers.get(qid)

        if selected is None:
            unanswered += 1

        if selected == q["correct_answer"]:
            score += 1

    return score, unanswered


# ------------------------------------------------------------
# EXAM TIMER
# ------------------------------------------------------------
def render_exam_timer():
    if not st.session_state.exam_mode or not st.session_state.exam_started_at or st.session_state.show_results:
        return

    if AUTOREFRESH_AVAILABLE:
        st_autorefresh(interval=1000, key="exam_timer_refresh")

    started = datetime.fromisoformat(st.session_state.exam_started_at)
    duration = timedelta(minutes=st.session_state.exam_duration_min)
    end_time = started + duration
    remaining = end_time - datetime.now()

    if remaining.total_seconds() <= 0:
        st.warning("Time is up. Your exam has been submitted.")
        st.session_state.show_results = True
        return

    mins = int(remaining.total_seconds() // 60)
    secs = int(remaining.total_seconds() % 60)

    st.info(f"⏱️ Time remaining: {mins:02d}:{secs:02d}")

    if not AUTOREFRESH_AVAILABLE:
        st.caption("Live countdown updates when the page reruns. Install streamlit-autorefresh for a live timer.")


# ------------------------------------------------------------
# MAIN APP
# ------------------------------------------------------------
def main():
    st.set_page_config(page_title="CISI Question Generator", layout="wide")
    init_session_state()
    init_auth_state()

    if not st.session_state.user:
        login_page()
        st.stop()

    st.sidebar.markdown("### Account")
    if st.session_state.profile:
        st.sidebar.write(f"**User:** {st.session_state.profile.get('display_name')}")
        st.sidebar.write(f"**Email:** {st.session_state.profile.get('email')}")
        st.sidebar.write(f"**Role:** {st.session_state.profile.get('role')}")
    else:
        st.sidebar.write("Profile not loaded")

    if st.sidebar.button("Sign out"):
        sign_out_user()

    profile = st.session_state.profile

    if not profile:
        st.error("No profile found for this account.")
        st.stop()

    if not profile.get("is_active", True):
        st.error("Your account is inactive. Contact the administrator.")
        st.stop()

    if not profile.get("can_generate_quizzes", True):
        st.error("Your account does not currently have quiz generation enabled.")
        st.stop()
    
    st.sidebar.title("Navigation")
    page = st.sidebar.radio(
        "Go to",
        ["Quiz", "Weak Topics", "Performance", "Wrong Answers Log"]
    )

    if not os.path.exists(JSON_FILE):
        st.error(f"Could not find {JSON_FILE} in this folder.")
        st.stop()

    try:
        records = load_records(JSON_FILE)
        sections = group_sections(records)
        chapter_map = build_chapter_map(sections)
        section_lookup = build_section_lookup(sections)
    except Exception as e:
        st.error(f"Failed to load parsed textbook JSON: {e}")
        st.stop()

    chapter_names = list(chapter_map.keys())

    # --------------------------------------------------------
    # QUIZ PAGE
    # --------------------------------------------------------
    if page == "Quiz":
        st.title("CISI Question Generator")
        st.write("Generate exam-style MCQs from your parsed textbook sections.")

        col1, col2 = st.columns([1, 2])

        with col1:
            st.subheader("Quiz setup")

            selected_chapters = st.multiselect(
                "Choose chapter(s)",
                chapter_names,
                default=[chapter_names[0]] if chapter_names else [],
            )

            visible_sections: List[Dict[str, Any]] = []
            for chapter in selected_chapters:
                visible_sections.extend(chapter_map.get(chapter, []))

            visible_labels = [section_label(s) for s in visible_sections]

            select_all_visible = st.checkbox(
                "Select all sections in chosen chapter(s)",
                value=False,
            )

            if select_all_visible:
                selected_section_labels = visible_labels
            else:
                selected_section_labels = st.multiselect(
                    "Choose section(s)",
                    visible_labels,
                    default=[],
                )

            selected_sections = [
                section_lookup[label] for label in selected_section_labels
            ]

            num_questions = st.slider(
                "Number of questions",
                min_value=5,
                max_value=30,
                value=10,
                step=5,
            )

            exam_mode = st.checkbox("Exam mode (hide answers until submission)", value=False)

            exam_duration_min = DEFAULT_EXAM_DURATION_MIN
            if exam_mode:
                exam_duration_min = st.slider(
                    "Exam duration (minutes)",
                    min_value=5,
                    max_value=90,
                    value=DEFAULT_EXAM_DURATION_MIN,
                    step=5,
                )

            show_source = st.checkbox("Show combined source text preview", value=False)

            weak_quiz_section_count = st.slider(
                "Weak-topic quiz: number of weak sections to pull from",
                min_value=1,
                max_value=5,
                value=3,
                step=1,
            )
            st.session_state.weak_quiz_section_count = weak_quiz_section_count

            generate_clicked = st.button("Generate from selected sections", type="primary")
            generate_weak_clicked = st.button("Generate from weakest areas")

            if st.button("Clear current quiz"):
                reset_quiz_state()
                st.rerun()

        with col2:
            st.subheader("Selection summary")

            if selected_chapters:
                st.write("**Chosen chapters:**")
                for ch in selected_chapters:
                    st.write(f"- {ch}")
            else:
                st.write("No chapters selected yet.")

            if selected_sections:
                st.write("**Chosen sections:**")
                for s in selected_sections:
                    st.write(
                        f"- {s['chapter_name']} | Section {s['section_number']} - {s['section_title']}"
                    )
            else:
                st.write("No sections selected yet.")

            if show_source and selected_sections:
                source_text, too_large = build_combined_source(selected_sections)
                if too_large:
                    st.warning(
                        f"The combined source is too large ({len(source_text):,} characters). "
                        f"Select fewer sections before generating."
                    )
                st.text_area(
                    "Combined source text preview",
                    source_text[:12000],
                    height=320,
                )

            if generate_clicked:
                if not selected_sections:
                    st.error("Please choose at least one section.")
                else:
                    source_text, too_large = build_combined_source(selected_sections)

                    if too_large:
                        st.error(
                            f"The selected text is too large ({len(source_text):,} characters). "
                            f"Please select fewer sections."
                        )
                    else:
                        with st.spinner("Generating questions..."):
                            try:
                                prompt = build_prompt(
                                    source_text=source_text,
                                    num_questions=num_questions,
                                    selected_sections=selected_sections,
                                    exam_mode=exam_mode,
                                )
                                result = call_openai_for_questions(prompt)
                                normalized_questions = normalize_question_payload(result, selected_sections)

                                st.session_state.generated_questions = normalized_questions
                                st.session_state.submitted_answers = {}
                                st.session_state.show_results = False
                                st.session_state.results_saved = False
                                st.session_state.quiz_id = str(uuid.uuid4())
                                st.session_state.quiz_generated_at = datetime.now().isoformat()
                                st.session_state.quiz_mode = "selected"
                                st.session_state.exam_mode = exam_mode
                                st.session_state.exam_duration_min = exam_duration_min
                                st.session_state.exam_started_at = datetime.now().isoformat() if exam_mode else None
                                st.session_state.selected_section_labels = selected_section_labels
                            except Exception as e:
                                st.error(f"Question generation failed: {e}")

            if generate_weak_clicked:
                weak_sections = select_weak_sections(
                    all_sections=sections,
                    max_sections=st.session_state.weak_quiz_section_count,
                )

                if not weak_sections:
                    st.error("No weak-topic data yet. Do a few quizzes first.")
                else:
                    source_text, too_large = build_combined_source(weak_sections)

                    if too_large:
                        st.error(
                            f"The weak-topic source is too large ({len(source_text):,} characters). "
                            f"Reduce the number of weak sections."
                        )
                    else:
                        with st.spinner("Generating weak-topic quiz..."):
                            try:
                                prompt = build_prompt(
                                    source_text=source_text,
                                    num_questions=num_questions,
                                    selected_sections=weak_sections,
                                    exam_mode=exam_mode,
                                )
                                result = call_openai_for_questions(prompt)
                                normalized_questions = normalize_question_payload(result, weak_sections)

                                st.session_state.generated_questions = normalized_questions
                                st.session_state.submitted_answers = {}
                                st.session_state.show_results = False
                                st.session_state.results_saved = False
                                st.session_state.quiz_id = str(uuid.uuid4())
                                st.session_state.quiz_generated_at = datetime.now().isoformat()
                                st.session_state.quiz_mode = "weak"
                                st.session_state.exam_mode = exam_mode
                                st.session_state.exam_duration_min = exam_duration_min
                                st.session_state.exam_started_at = datetime.now().isoformat() if exam_mode else None
                                st.session_state.selected_section_labels = [section_label(s) for s in weak_sections]
                                st.success("Weak-topic quiz generated.")
                            except Exception as e:
                                st.error(f"Weak-topic question generation failed: {e}")

        questions = st.session_state.generated_questions

        if questions:
            st.divider()
            st.subheader("Questions")

            if st.session_state.quiz_mode == "weak":
                st.caption("This quiz was generated from your weakest or due-for-review topics.")

            render_exam_timer()

            for i, q in enumerate(questions, start=1):
                render_question_block(q, i)
                st.markdown("---")

            col_a, col_b, col_c = st.columns(3)

            with col_a:
                if not st.session_state.exam_mode:
                    if st.button("Mark my answers"):
                        st.session_state.show_results = True
                        st.rerun()
                else:
                    if st.button("Submit exam now"):
                        st.session_state.show_results = True
                        st.rerun()

            with col_b:
                if st.button("Clear answers"):
                    st.session_state.submitted_answers = {}
                    st.session_state.show_results = False
                    st.session_state.results_saved = False
                    for i in range(1, len(questions) + 1):
                        radio_key = f"radio_q_{i}"
                        if radio_key in st.session_state:
                            del st.session_state[radio_key]
                    st.rerun()

            with col_c:
                if st.button("Start a fresh quiz"):
                    reset_quiz_state()
                    st.rerun()

            if st.session_state.show_results and not st.session_state.results_saved:
                persist_quiz_results(
                    questions=questions,
                    submitted_answers=st.session_state.submitted_answers,
                    quiz_id=st.session_state.quiz_id or str(uuid.uuid4()),
                )
                st.session_state.results_saved = True

            if st.session_state.show_results:
                score, unanswered = score_answers(questions)
                st.success(f"Score: {score} / {len(questions)}")
                if unanswered:
                    st.warning(f"Unanswered questions: {unanswered}")

    # --------------------------------------------------------
    # WEAK TOPICS PAGE
    # --------------------------------------------------------
    elif page == "Weak Topics":
        st.title("Weak Topics")
        st.write("These are ranked using your wrong answers, accuracy, and spaced-repetition due dates.")

        rankings = build_weak_topic_rankings(sections)

        display_rows = []
        for row in rankings:
            if row["attempts"] == 0 and not row["due_for_review"] and row["wrong"] == 0:
                continue

            display_rows.append({
                "Chapter": row["chapter_name"],
                "Section": row["section_number"],
                "Title": row["section_title"],
                "Attempts": row["attempts"],
                "Wrong": row["wrong"],
                "Accuracy %": row["accuracy"],
                "Due for review": row["due_for_review"],
                "Next review": row["next_review_at"],
                "Weak score": row["weak_score"],
            })

        if not display_rows:
            st.info("No weak-topic data yet. Complete a few quizzes first.")
        else:
            st.dataframe(display_rows, use_container_width=True)

    # --------------------------------------------------------
    # PERFORMANCE PAGE
    # --------------------------------------------------------
    elif page == "Performance":
        st.title("Performance Dashboard")

        summary = build_overall_summary()
        stats = build_attempt_stats()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total attempts", summary["total_attempts"])
        c2.metric("Correct", summary["correct"])
        c3.metric("Wrong", summary["wrong"])
        c4.metric("Accuracy", f"{summary['accuracy']}%" if summary["accuracy"] is not None else "N/A")

        rows = []
        for _, item in stats.items():
            rows.append({
                "Chapter": item["chapter_name"],
                "Section": item["section_number"],
                "Title": item["section_title"],
                "Attempts": item["attempts"],
                "Correct": item["correct"],
                "Wrong": item["wrong"],
                "Accuracy %": item["accuracy"],
                "Review stage": item["review_stage"],
                "Due for review": item["due_for_review"],
                "Next review": item["next_review_at"],
                "Last attempt": item["last_attempt_at"],
            })

        rows.sort(key=lambda x: (x["Accuracy %"] if x["Accuracy %"] is not None else 999))
        if rows:
            st.dataframe(rows, use_container_width=True)
        else:
            st.info("No attempt data yet. Complete a quiz first.")

    # --------------------------------------------------------
    # WRONG ANSWERS LOG PAGE
    # --------------------------------------------------------
    elif page == "Wrong Answers Log":
        st.title("Wrong Answers Log")

        wrongs = recent_wrong_answers(limit=100)

        if not wrongs:
            st.info("No wrong answers saved yet.")
        else:
            st.write(f"Showing {len(wrongs)} most recent mistakes.")
            for i, item in enumerate(wrongs, start=1):
                header = (
                    f"{i}. {item.get('chapter_name')} | "
                    f"Section {item.get('section_number')} - "
                    f"{item.get('section_title')} | "
                    f"{item.get('timestamp')}"
                )
                with st.expander(header):
                    st.write(f"**Question:** {item.get('question_text')}")
                    st.write(f"**Your answer:** {item.get('selected_answer')}")
                    st.write(f"**Correct answer:** {item.get('correct_answer')}")
                    st.write(f"**Explanation:** {item.get('explanation')}")
                    st.write(f"**Source reference:** {item.get('source_reference')}")

                    feedback = item.get("selected_option_feedback")
                    if feedback:
                        st.write(f"**Why your chosen answer was wrong:** {feedback}")

                    options = item.get("options", {})
                    if options:
                        st.write("**Options:**")
                        for label in ["A", "B", "C", "D"]:
                            st.write(f"{label}. {options.get(label, '')}")


if __name__ == "__main__":
    main()
