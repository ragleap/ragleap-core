package com.ragleap.vectorstores;

import java.util.List;
import java.util.StringJoiner;

/** Python repr() of a string and of a list of strings, for the error messages that Python builds with {x!r} or {list}. */
final class PyRepr {
    private PyRepr() {
    }

    /** repr(str): single quotes, or double quotes when the text has a single but no double quote; backslash escapes. */
    static String str(String s) {
        boolean hasSingle = s.indexOf('\'') >= 0;
        boolean hasDouble = s.indexOf('"') >= 0;
        final char quote = hasSingle && !hasDouble ? '"' : '\'';
        StringBuilder sb = new StringBuilder().append(quote);
        s.codePoints().forEach(cp -> {
            if (cp == quote || cp == '\\') {
                sb.append('\\').appendCodePoint(cp);
            } else if (cp == '\n') {
                sb.append("\\n");
            } else if (cp == '\r') {
                sb.append("\\r");
            } else if (cp == '\t') {
                sb.append("\\t");
            } else if (cp < 0x20 || cp == 0x7f) {
                sb.append(String.format("\\x%02x", cp));
            } else {
                sb.appendCodePoint(cp);
            }
        });
        return sb.append(quote).toString();
    }

    /** repr(list of str): ['a', 'b']. */
    static String list(List<String> items) {
        StringJoiner j = new StringJoiner(", ", "[", "]");
        for (String s : items) {
            j.add(str(s));
        }
        return j.toString();
    }
}
