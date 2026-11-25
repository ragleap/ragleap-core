package com.ragleap.rag.parsers;

import java.math.BigDecimal;

/** Formats a double exactly like Python's repr(float) / str(float). Needs JDK 19+ shortest-digit Double.toString. */
public final class PyFloat {

    private PyFloat() {
    }

    public static String repr(double d) {
        if (Double.isNaN(d)) {
            return "nan";
        }
        if (Double.isInfinite(d)) {
            return d > 0 ? "inf" : "-inf";
        }
        if (d == 0) {
            return (1 / d < 0) ? "-0.0" : "0.0";
        }
        String sign = d < 0 ? "-" : "";
        BigDecimal bd = new BigDecimal(Double.toString(Math.abs(d))).stripTrailingZeros();
        String digits = bd.unscaledValue().toString();
        int decpt = digits.length() - bd.scale();
        if (decpt > -4 && decpt <= 16) {
            if (decpt <= 0) {
                return sign + "0." + "0".repeat(-decpt) + digits;
            }
            if (decpt >= digits.length()) {
                return sign + digits + "0".repeat(decpt - digits.length()) + ".0";
            }
            return sign + digits.substring(0, decpt) + "." + digits.substring(decpt);
        }
        int exp = decpt - 1;
        String mantissa = digits.length() > 1 ? digits.charAt(0) + "." + digits.substring(1) : digits;
        return sign + mantissa + "e" + (exp < 0 ? "-" : "+") + (Math.abs(exp) < 10 ? "0" : "") + Math.abs(exp);
    }
}
