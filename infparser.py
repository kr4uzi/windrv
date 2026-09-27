"""
Windows INF file parser implementing the Microsoft INF syntax rules:
https://github.com/MicrosoftDocs/windows-driver-docs/blob/staging/windows-driver-docs-pr/install/general-syntax-rules-for-inf-files.md
https://github.com/MicrosoftDocs/windows-driver-docs/blob/staging/windows-driver-docs-pr/install/inf-strings-section.md
"""
import codecs
import pathlib
import re

MAX_SECTION_NAME = 255
# 4096 characters including the terminating NULL
MAX_FIELD = 4095

_STRKEY_RE = re.compile(r'%([^%\s]*)%')


def decode(data: bytes) -> str:
    # INF files must be saved with Unicode (UTF-16 LE) or ANSI file encoding
    # https://learn.microsoft.com/windows-hardware/drivers/display/general-unicode-requirement
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16")
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        # cp1252 leaves a few bytes undefined, latin-1 maps every byte
        return data.decode("latin-1")


def unquote(field: str) -> str:
    # The outermost pair of enclosing double quotes is discarded and each
    # subsequent pair of double quotes is condensed into a single one.
    # Whitespace outside the quotes is discarded as well.
    s = field.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('""', '"')

    if '"' not in s:
        return s

    # partially quoted field (e.g. abc"d;ef"): drop the quote delimiters
    out = []
    inQuote = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == '"':
            if inQuote and s[i + 1:i + 2] == '"':
                out.append('"')
                i += 2
                continue
            inQuote = not inQuote
        else:
            out.append(c)
        i += 1
    return ''.join(out)


def splitFields(value: str) -> list[str]:
    # Commas separate values; optional values in the middle keep their commas,
    # commas inside a "quoted string" are part of the value.
    fields = []
    current = []
    inQuote = False
    for c in value:
        if c == '"':
            inQuote = not inQuote
        elif c == ',' and not inQuote:
            fields.append(''.join(current))
            current = []
            continue
        current.append(c)
    fields.append(''.join(current))
    return fields


def _stripComment(line: str) -> tuple[str, bool]:
    # A semicolon starts a comment unless it appears within a "quoted string"
    # or a %strkey% token. Returns the content and whether a quote is left open.
    out = []
    inQuote = False
    i = 0
    while i < len(line):
        c = line[i]
        if c == '"':
            inQuote = not inQuote
        elif not inQuote and c == '%':
            m = _STRKEY_RE.match(line, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
        elif not inQuote and c == ';':
            break
        out.append(c)
        i += 1
    return ''.join(out), inQuote


def _splitKey(line: str) -> tuple[str | None, str]:
    inQuote = False
    for i, c in enumerate(line):
        if c == '"':
            inQuote = not inQuote
        elif c == '=' and not inQuote:
            return line[:i], line[i + 1:]
    return None, line


class Entry:
    # key: expanded key (None for lines without '=', e.g. file lists)
    key: str | None
    rawKey: str | None
    # values: unquoted and %strkey% expanded
    values: list[str]
    # rawValues: unquoted only (file names must not be expanded)
    rawValues: list[str]
    # rawValue: everything after '=' (or the whole line), comment removed
    rawValue: str
    line: int

    def __init__(self, rawKey: str | None, rawValue: str, line: int):
        self.rawKey = unquote(rawKey) if rawKey is not None else None
        self.key = self.rawKey
        self.rawValue = rawValue.strip()
        self.rawValues = [unquote(f) for f in splitFields(self.rawValue)] if self.rawValue else []
        self.values = self.rawValues
        self.line = line

    def __repr__(self):
        return f"Entry({self.key!r}, {self.values!r}, line={self.line})"


class Section:
    name: str
    entries: list[Entry]

    def __init__(self, name: str):
        self.name = name
        self.entries = []


class InfFile:
    path: str
    # lowercased section name -> Section (duplicate sections are merged)
    sections: dict[str, Section]
    # lowercased strkey -> value of the selected Strings section
    strings: dict[str, str]
    stringsSection: str | None
    warnings: list[str]

    def __init__(self):
        self.path = ""
        self.sections = {}
        self.strings = {}
        self.stringsSection = None
        self.warnings = []

    @classmethod
    def load(cls, source: str | pathlib.Path | bytes, name: str | None = None, language: str | None = None) -> "InfFile":
        inf = cls()
        if isinstance(source, bytes):
            data = source
            inf.path = name or "<bytes>"
        else:
            data = pathlib.Path(source).read_bytes()
            inf.path = name or str(source)

        inf._parse(decode(data))
        inf._selectStrings(language)
        inf._expand()
        return inf

    def warn(self, line: int | None, message: str):
        self.warnings.append(f"{self.path}:{line}: {message}" if line else f"{self.path}: {message}")

    def has(self, name: str) -> bool:
        return name.lower() in self.sections

    def section(self, name: str) -> Section | None:
        return self.sections.get(name.lower())

    def entries(self, name: str) -> list[Entry]:
        section = self.sections.get(name.lower())
        return section.entries if section else []

    def sectionNames(self) -> list[str]:
        return list(self.sections.keys())

    def expand(self, value: str, line: int | None = None) -> str:
        # %strkey% tokens are replaced by the selected Strings section, %% is a literal %
        def replace(m):
            key = m.group(1)
            if not key:
                return '%'
            if key.lower() in self.strings:
                return self.strings[key.lower()]
            # numeric tokens are dirids (e.g. %13%) which are resolved at install time
            if not key.isdigit():
                section = self.sections[self.stringsSection].name if self.stringsSection else "Strings"
                self.warn(line, f"string token %{key}% not defined in [{section}]")
            return m.group(0)

        return _STRKEY_RE.sub(replace, value)

    def resolveDecorated(self, name: str, arch: str | None) -> str | None:
        # https://learn.microsoft.com/windows-hardware/drivers/install/creating-inf-files-for-multiple-platforms-and-operating-systems
        # section-name.nt<architecture> -> section-name.nt -> section-name
        name = name.lower()
        candidates = [f"{name}.nt{arch.lower()}"] if arch else []
        candidates += [f"{name}.nt", name]
        for candidate in candidates:
            if candidate in self.sections:
                return candidate
        return None

    def _parse(self, text: str):
        current = None
        pending = None  # (start line, accumulated content) of a continued line

        for lineNo, raw in enumerate(text.splitlines(), start=1):
            if pending is None and raw.lstrip().startswith('['):
                current = self._parseSectionHeader(raw, lineNo)
                continue

            content, openQuote = _stripComment(raw)
            if openQuote:
                self.warn(lineNo, "unclosed quote")

            stripped = content.rstrip()
            if stripped.endswith('\\') and not openQuote:
                # explicit line continuation; with a trailing "\\" the first
                # backslash is ignored and the second one is the continuator
                stripped = stripped[:-1]
                if stripped.endswith('\\'):
                    stripped = stripped[:-1]
                pending = (pending[0] if pending else lineNo, (pending[1] if pending else "") + stripped)
                continue

            startLine = lineNo
            if pending:
                startLine, content = pending[0], pending[1] + content
                pending = None

            if not content.strip():
                continue

            if current is None:
                self.warn(startLine, "line does not belong to any section")
                continue

            key, value = _splitKey(content)
            entry = Entry(key, value, startLine)
            for field in [entry.rawKey or ""] + entry.rawValues:
                if len(field) > MAX_FIELD:
                    self.warn(startLine, f"field exceeds {MAX_FIELD + 1} characters")
            current.entries.append(entry)

        if pending:
            self.warn(pending[0], "line continuation at end of file")
            if current is not None:
                key, value = _splitKey(pending[1])
                current.entries.append(Entry(key, value, pending[0]))

    def _parseSectionHeader(self, raw: str, lineNo: int) -> Section | None:
        # a section name may contain any character except the closing bracket
        line = raw.lstrip()
        end = line.find(']')
        if end < 0:
            self.warn(lineNo, "section header without closing bracket")
            return None

        rest, _ = _stripComment(line[end + 1:])
        if rest.strip():
            self.warn(lineNo, f"unexpected content after section header: {rest.strip()}")

        name = line[1:end].strip()
        if len(name) > MAX_SECTION_NAME:
            self.warn(lineNo, f"section name exceeds {MAX_SECTION_NAME} characters")

        # If more than one section has the same name, the entries are merged
        normalized = name.lower()
        if normalized not in self.sections:
            self.sections[normalized] = Section(name)
        return self.sections[normalized]

    def _selectStrings(self, language: str | None):
        # Windows selects a single Strings section to translate all %strkey% tokens:
        # exact Strings.LanguageID, then the neutral sublanguage, then any sublanguage
        # of the same primary language, then the undecorated Strings section.
        # Without a requested language, the undecorated (English) section is preferred.
        localized = [n for n in self.sections if n.startswith("strings.")]
        selected = None
        if language:
            language = language.lower()
            primary = int(language, 16) & 0x3FF
            exact = f"strings.{language}"
            if exact in self.sections:
                selected = exact
            else:
                for n in localized:
                    try:
                        langId = int(n[len("strings."):], 16)
                    except ValueError:
                        continue
                    if langId & 0x3FF == primary and (selected is None or langId >> 10 == 0):
                        selected = n

        if selected is None:
            if "strings" in self.sections:
                selected = "strings"
            elif "strings.0409" in self.sections:
                selected = "strings.0409"
            elif localized:
                selected = localized[0]

        self.stringsSection = selected
        if selected is None:
            return

        for entry in self.sections[selected].entries:
            if entry.rawKey is None:
                self.warn(entry.line, "strings entry without key")
                continue
            key = entry.rawKey.lower()
            if key in self.strings:
                self.warn(entry.line, f"duplicate string key {entry.rawKey}")
            # a string value is not a comma separated list
            self.strings[key] = unquote(entry.rawValue)

    def _expand(self):
        for name, section in self.sections.items():
            if name == "strings" or name.startswith("strings."):
                continue
            for entry in section.entries:
                if entry.rawKey is not None:
                    entry.key = self.expand(entry.rawKey, entry.line)
                entry.values = [self.expand(v, entry.line) for v in entry.rawValues]
