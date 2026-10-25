# ragleap-terraform (Java jar)

The Terraform module files of [ragleap-terraform](https://pypi.org/project/ragleap-terraform/)
packaged as a jar, with a small class that reads them:

```java
String main = com.ragleap.terraform.RagleapTerraform.readString("modules/kind-cluster/main.tf");
```

This is **not a code port**: the jar carries the same files as the PyPI wheel, under the same
paths, and `.github/scripts/java-terraform-parity.sh` compares the two (file list and file
contents). Terraform itself is not included or run by this jar.

**Status:** not published to Maven Central yet. The build and the parity check run in the manual
`java-terraform-test` workflow. Publishing needs the owner's GPG key and Central token:
`mvn clean deploy -Prelease` from this folder, as for the other Java libraries.

The version mirrors the PyPI package (the parity script fails if they differ).
