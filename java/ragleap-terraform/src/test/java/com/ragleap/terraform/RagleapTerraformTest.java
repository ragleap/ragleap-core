package com.ragleap.terraform;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

class RagleapTerraformTest {

    @Test
    void allFourModuleFilesAreBundledAndNotEmpty() {
        for (String f : new String[] {"main.tf", "outputs.tf", "variables.tf", "versions.tf"}) {
            assertFalse(RagleapTerraform.readString("modules/kind-cluster/" + f).isBlank(), f);
        }
    }

    @Test
    void theExampleIsBundled() {
        assertFalse(RagleapTerraform.readString("examples/kind/main.tf").isBlank());
    }

    @Test
    void aMissingFileIsRejected() {
        assertThrows(IllegalArgumentException.class, () -> RagleapTerraform.open("modules/nope.tf"));
    }

    @Test
    void parentPathsAreRejected() {
        assertThrows(IllegalArgumentException.class, () -> RagleapTerraform.open("../x"));
    }
}
