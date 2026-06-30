"""
occuralog.parser
----------------
WhatsApp export parser — v0.1

Parses a WhatsApp group export (.txt) into structured, timestamped JSON events.
Rule-based classification covers ~60% of cases. Unclassified messages are logged
as event_type: "unknown" for LLM fallback in a future pipeline stage.

Usage:
    from occuralog.parser import WhatsAppParser

    parser = WhatsAppParser("tests/fixtures/manufacturing_whatsapp_sample.txt")
    events = parser.parse()
    parser.save("output/events.json")
"""

import re
import json
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Regex — WhatsApp message line format
# Handles both 12h and 24h, with and without seconds
# Example: [1/8/2024, 9:02 AM] Tariq Usman: order 3201 ka material kab aayega?
# ---------------------------------------------------------------------------
MESSAGE_PATTERN = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{4}),\s(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)\]\s(.+?):\s(.+)$"
)


# ---------------------------------------------------------------------------
# Attachment detector — lines like [Document: punchlist_5600_final.pdf]
# Structural, not keyword-based, so it's checked before the keyword rules.
# ---------------------------------------------------------------------------
ATTACHMENT_PATTERN = re.compile(r"^\[Document:\s*.+?\]$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Rule sets — keyword lists for each event type
# Urdu/Roman Urdu + English, as found in Pakistani ops groups
# ---------------------------------------------------------------------------
RULES = {
    "delivery_delay": [
        "nahi aaya", "nahi aya", "nahi aayi", "nahi ayi",
        "delay", "late", "delayed", "truck nahi",
        "pohoncha nahi", "abhi tak nahi", "breakdown",
        "puncture", "accident", "ruka hua", "ruki hui",
        "nahi pohoncha", "deliver nahi",
    ],
    "material_shortage": [
        "khatam", "khatam ho gaya", "shortage", "nahi mil rahi",
        "nahi hai", "stock nahi", "available nahi",
        "market mein nahi", "material nahi",
        "gas supply band", "supply band",
    ],
    "quality_rejection": [
        "rejected", "reject", "wrong gauge", "wrong size",
        "tolerance", "NCR", "defect", "rework", "qaabil nahi",
        "sahi nahi", "quality issue", "out of spec",
    ],
    "dispatch_confirmation": [
        "dispatch", "dispatched", "dispatch ho gaya", "dispatch ✅",
        "truck book", "loading", "nikla", "nikal gaya",
        "bhej diya", "send kar diya", "tracking",
        "truck chal pada", "container clear", "material aa gaya", "material aa gaye",
    ],
    "production_status": [
        "production resume", "production start", "production band",
        "production ruk", "complete", "fabrication complete",
        "welding start", "cutting complete", "painting",
        "floor ready", "kaam shuru", "kaam band",
        "factory band", "factory resume", "shift lagani", "operations resume",
        "production normal", "winter shutdown", "gas aa gaya", "team puri aa gayi",
        "bijli aa gayi",
    ],
    "payment_event": [
        "payment", "advance", "transfer", "payment aa gayi",
        "payment nahi", "payment rok", "invoice",
        "amount", "wire transfer", "credit",
    ],
    "safety_incident": [
        "accident", "injury", "injured", "cut", "haath",
        "hospital", "clinic", "first aid", "safety",
        "incident report", "warning", "gloves",
        "stitches",
    ],
    "design_change": [
        "drawing", "revision", "design change", "discrepancy",
        "change order", "variation order", "scope",
        "update aayegi", "drawing mein",
    ],
    "vendor_event": [
        "vendor", "supplier", "price revise", "rate",
        "contract", "RFQ", "quotation", "Al-Kareem",
        "Hamza Steel", "Sindh Steel", "order diya",
        "naya order aaya", "order confirm hua", "order karo", "order de diya",
    ],
    "qc_inspection": [
        "QC", "inspection", "inspector", "passed", "failed",
        "punch list", "site visit", "client engineer",
        "approved", "sign off",
        "client visit", "punchlist", "certificates",
    ],
}


# ---------------------------------------------------------------------------
# Order number extractor
# ---------------------------------------------------------------------------
ORDER_PATTERN = re.compile(r"\border\s*#?(\d{3,5})\b", re.IGNORECASE)

# Short acknowledgment / filler patterns — these are context-dependent replies
# that rule-based keyword matching cannot meaningfully classify on their own.
ACK_PATTERNS = [
    "theek hai", "👍", "haan", "ok", "okay", "accha", "samajh gaya",
]

SHORT_MESSAGE_THRESHOLD = 25  # characters


def needs_llm_review(text: str, event_types: list[str]) -> bool:
    """
    Flags unknown messages that are short or ack-like, meaning they need
    thread context (and likely an LLM) to classify — as opposed to longer
    unknown messages that might still be addressable with better keyword rules.
    """
    if event_types != ["unknown"]:
        return False

    stripped = text.strip()
    if len(stripped) <= SHORT_MESSAGE_THRESHOLD:
        return True

    text_lower = stripped.lower()
    return any(pattern in text_lower for pattern in ACK_PATTERNS)


def extract_order_numbers(text: str) -> list[str]:
    return ORDER_PATTERN.findall(text)


# ---------------------------------------------------------------------------
# Timestamp normaliser
# ---------------------------------------------------------------------------
TIMESTAMP_FORMATS = [
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
]

def parse_timestamp(date_str: str, time_str: str) -> str | None:
    raw = f"{date_str} {time_str.strip()}"
    for fmt in TIMESTAMP_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Rule-based classifier
# Returns a list — a message can match multiple event types
# ---------------------------------------------------------------------------
def classify(text: str) -> list[str]:
    if ATTACHMENT_PATTERN.match(text.strip()):
        return ["document_attachment"]

    text_lower = text.lower()
    matched = []
    for event_type, keywords in RULES.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                matched.append(event_type)
                break
    return matched if matched else ["unknown"]


# ---------------------------------------------------------------------------
# Parser class
# ---------------------------------------------------------------------------
class WhatsAppParser:
    """
    Parses a WhatsApp group export into structured events.

    Args:
        filepath: Path to the .txt WhatsApp export file.

    Example:
        parser = WhatsAppParser("tests/fixtures/manufacturing_whatsapp_sample.txt")
        events = parser.parse()
        parser.save("output/events.json")
    """

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)
        self.events: list[dict] = []
        self.stats: dict = {}

    def parse(self) -> list[dict]:
        """
        Read and parse the export file. Returns list of structured event dicts.
        """
        if not self.filepath.exists():
            raise FileNotFoundError(f"File not found: {self.filepath}")

        raw_lines = self.filepath.read_text(encoding="utf-8").splitlines()

        parsed_messages = []
        for line in raw_lines:
            # Skip comments and blank lines
            if line.startswith("#") or not line.strip():
                continue

            match = MESSAGE_PATTERN.match(line)
            if not match:
                continue

            date_str, time_str, sender, text = match.groups()
            timestamp = parse_timestamp(date_str, time_str)
            event_types = classify(text)
            order_numbers = extract_order_numbers(text)
            llm_review = needs_llm_review(text, event_types)

            event = {
                "timestamp": timestamp,
                "timestamp_raw": f"{date_str} {time_str}",
                "sender": sender.strip(),
                "text": text.strip(),
                "event_types": event_types,
                "order_numbers": order_numbers,
                "classified": event_types != ["unknown"],
                "needs_llm_review": llm_review,
                "source": "whatsapp_export",
            }

            parsed_messages.append(event)

        self.events = parsed_messages
        self._compute_stats()
        return self.events

    def _compute_stats(self):
        total = len(self.events)
        classified = sum(1 for e in self.events if e["classified"])
        unknown = total - classified
        flagged_for_review = sum(1 for e in self.events if e["needs_llm_review"])
        unknown_needs_context = unknown - flagged_for_review

        type_counts: dict[str, int] = {}
        for event in self.events:
            for et in event["event_types"]:
                type_counts[et] = type_counts.get(et, 0) + 1

        self.stats = {
            "total_messages": total,
            "classified": classified,
            "unknown": unknown,
            "unknown_flagged_for_llm_review": flagged_for_review,
            "unknown_possibly_rule_classifiable": unknown_needs_context,
            "classification_rate": round(classified / total * 100, 1) if total else 0,
            "event_type_counts": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
        }

    def save(self, output_path: str):
        """
        Save parsed events + stats to a JSON file.
        """
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "metadata": {
                "source_file": str(self.filepath),
                "parser_version": "0.1.0",
                "stats": self.stats,
            },
            "events": self.events,
        }

        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved {len(self.events)} events to {out}")
        print(f"Classification rate: {self.stats['classification_rate']}%")
        print(f"Event type breakdown: {self.stats['event_type_counts']}")

    def summary(self):
        """Print a quick stats summary to stdout."""
        if not self.stats:
            print("No data parsed yet. Run .parse() first.")
            return
        print("\n--- occuralog parser v0.1 ---")
        print(f"Total messages   : {self.stats['total_messages']}")
        print(f"Classified       : {self.stats['classified']}")
        print(f"Unknown          : {self.stats['unknown']}")
        print(f"  -> flagged for LLM review : {self.stats['unknown_flagged_for_llm_review']}")
        print(f"  -> possibly rule-fixable  : {self.stats['unknown_possibly_rule_classifiable']}")
        print(f"Classification % : {self.stats['classification_rate']}%")
        print("\nEvent type breakdown:")
        for et, count in self.stats["event_type_counts"].items():
            print(f"  {et:<25} {count}")


# ---------------------------------------------------------------------------
# CLI entry point — python -m occuralog.parser <input> <output>
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python parser.py <whatsapp_export.txt> [output.json]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else "output/events.json"

    parser = WhatsAppParser(input_file)
    parser.parse()
    parser.summary()
    parser.save(output_file)