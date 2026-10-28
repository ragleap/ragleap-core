# Ragleap Ops — Java JAR

This Maven project packages both resource trees from `packages/ragleap-ops`
into a Java JAR. Its version must match the Python package version and the
Helm chart metadata.

Bundled paths:

- `ragleap_ops/helm/ragleap-ops/...`
- `ragleap_ops/k8s/...`

## Read resources

```java
import com.ragleap.ops.RagleapOps;

String chart = RagleapOps.readHelmString("ragleap-ops/Chart.yaml");
String deployment = RagleapOps.readK8sString("app-deployment.yaml");
```

The `openHelm` and `openK8s` methods return streams that callers must close.
The `readHelmString` and `readK8sString` methods read UTF-8 text. Resource
paths are relative to their respective roots; absolute paths and parent
directory traversal are rejected.

Reading resources does not install a Helm chart or apply Kubernetes resources.

## Build and test

From this directory:

```bash
mvn -B verify
```

The build creates the main JAR and attaches source and Javadoc JARs. It does
not publish artifacts or deploy resources.

## Python-wheel parity

From the repository root:

```bash
bash .github/scripts/java-ops-parity.sh
```

The parity script is intended for GitHub Actions and refuses to run elsewhere
unless `FORCE=1` is explicitly supplied. It builds the Python wheel and Maven
artifacts and compares the complete Helm and Kubernetes resource trees,
including file lists and contents. Negative break-proof checks verify that
missing and modified files are detected. The script does not publish packages,
deploy Helm, apply manifests, or modify a cluster.
