package com.ragleap.appchart;

import static org.junit.jupiter.api.Assertions.*;

import java.io.IOException;
import org.junit.jupiter.api.Test;

class RagleapAppChartTest {
    @Test
    void readsChartMetadata() throws IOException {
        String chart = RagleapAppChart.readString("ragleap-app-chart/Chart.yaml");

        assertTrue(chart.contains("name: ragleap-app-chart"), chart);
        assertTrue(chart.matches("(?s).*\\bversion:\\s*['\"]?0\\.3\\.2['\"]?.*"), chart);
        assertTrue(chart.matches("(?s).*\\bappVersion:\\s*['\"]?0\\.3\\.2['\"]?.*"), chart);
    }

    @Test
    void readsValuesAndTemplates() throws IOException {
        assertFalse(RagleapAppChart.readString("ragleap-app-chart/values.yaml").isBlank());
        assertFalse(RagleapAppChart.readString(
                "ragleap-app-chart/templates/deployment.yaml").isBlank());
    }

    @Test
    void rejectsMissingResource() {
        assertThrows(IOException.class,
                () -> RagleapAppChart.open("ragleap-app-chart/not-a-real-file.yaml"));
    }

    @Test
    void rejectsNullAndEmptyPaths() {
        assertThrows(NullPointerException.class, () -> RagleapAppChart.open(null));
        assertThrows(IllegalArgumentException.class, () -> RagleapAppChart.open(""));
        assertThrows(IllegalArgumentException.class, () -> RagleapAppChart.open("  "));
    }

    @Test
    void rejectsAbsoluteAndParentPaths() {
        assertThrows(IllegalArgumentException.class,
                () -> RagleapAppChart.open("/etc/passwd"));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapAppChart.open("../pom.xml"));
        assertThrows(IllegalArgumentException.class,
                () -> RagleapAppChart.open("ragleap-app-chart/../../pom.xml"));
    }
}
