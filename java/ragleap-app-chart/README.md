# Ragleap App Chart — Java JAR

This Maven project packages the Helm chart resources from
`packages/ragleap-app-chart/helm` into a Java JAR. Its version must match the
Python package version in `packages/ragleap-app-chart/pyproject.toml`.

The chart files are stored in the JAR under:

`ragleap_app_chart/helm/ragleap-app-chart/...`

## Read a resource

```java
import com.ragleap.appchart.RagleapAppChart;

String chartYaml =
    RagleapAppChart.readString("ragleap-app-chart/Chart.yaml");
```

`open(relativePath)` returns an `InputStream` that the caller must close.
`readString(relativePath)` reads UTF-8 text. Paths are relative to the bundled
Helm directory and parent-directory traversal is rejected.

## Build and test

From this directory:

```bash
mvn -B verify
```

The build creates the main JAR and attaches source and Javadoc JARs. It does
not publish artifacts or install a Helm release.

## Python-wheel parity

From the repository root, run:

```bash
bash .github/scripts/java-app-chart-parity.sh
```

The parity script is intended for GitHub Actions and refuses to run elsewhere
unless `FORCE=1` is explicitly supplied. It builds the Python wheel and Maven
artifacts, compares the chart resource file lists and contents, and runs
negative break-proof checks for a missing file and modified file. It does not
publish packages, deploy Helm, or modify a cluster.
