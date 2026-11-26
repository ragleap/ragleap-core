package com.ragleap.rag;

/** Thrown when a RagLeap built with requireTenant(true) gets a call without a tenant scope. */
public class TenantRequiredException extends IllegalStateException {
    public TenantRequiredException(String message) {
        super(message);
    }
}
