# Vendor Integration Notes

This document provides guidance for vendors integrating this implementation into commercial or operational 911 systems.

The goal is to ensure interoperability, stability, and long-term compatibility across implementations.

---

## 1. Purpose of This Implementation

This project provides a reference implementation for 911 data exchange and mapping interoperability. It is intended to:

- Support CAD and mapping system integration
- Enable standardized spatial and event data exchange
- Serve as a vendor-neutral reference implementation

It is not intended to replace proprietary CAD systems.

---

## 2. Integration Model

This system is designed to be integrated via:

- REST APIs
- Event-based messaging (where applicable)
- GIS layer services
- Optional embedded SDK components

Direct modification of core logic is discouraged for production integrations.

---

## 3. Stability Expectations

The following components are considered stable:

- Public API endpoints
- Data schemas
- Core event definitions
- Geographic data structures

The following may evolve:

- Internal processing logic
- Reference UI components
- Experimental modules

Breaking changes will be versioned using semantic versioning.

---

## 4. Data Model Guidelines

Vendors should adhere to the published schemas located in `/schemas`.

Rules:

- Do not extend required fields without namespace separation
- Preserve field types exactly as defined
- Unknown fields should be ignored, not rejected
- Timestamp format: ISO 8601

---

## 5. Interoperability Principles

This system assumes:

- Multiple vendors may operate on the same dataset
- Data may be merged from multiple jurisdictions
- Latency-sensitive operations may occur (real-time dispatch environments)

Therefore:

- Avoid hard dependencies on synchronous responses
- Design for eventual consistency where applicable
- Do not assume single-source-of-truth control

---

## 6. Extension Guidelines

Vendors may extend the system using:

- Namespaced fields (e.g. `vendorX_customField`)
- Separate sidecar services
- External enrichment pipelines

Do not modify core schema definitions directly for vendor-specific needs.

---

## 7. Performance Considerations

This system may be used in real-time operational environments.

Recommended:

- Cache static GIS layers locally where possible
- Use incremental updates instead of full dataset refreshes
- Avoid blocking API calls in dispatch-critical paths

---

## 8. Security & Data Handling

Vendors are responsible for:

- Securing any deployed integration endpoints
- Ensuring encryption in transit (TLS required)
- Complying with CJIS and applicable state/federal requirements

This project does not provide security accreditation for production deployments.

---

## 9. Versioning & Compatibility

- API versioning follows semantic versioning (MAJOR.MINOR.PATCH)
- Breaking changes will increment MAJOR version
- Deprecated endpoints will remain available for at least one major version cycle

---

## 10. Reference Implementation

This repository is considered the reference implementation. Vendors are encouraged to:

- Fork for experimentation
- Build adapters around it rather than modifying core logic
- Contribute improvements upstream when applicable

---

## 11. Support

Support is provided on a best-effort basis via GitHub Issues.

For vendor-specific integration discussions, open an issue tagged `vendor-integration`.