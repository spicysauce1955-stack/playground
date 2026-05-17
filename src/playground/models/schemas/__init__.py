"""Reserved location for JSON Schema exports of the contract models.

Per ``ai/architecture/shared_contracts.md §12`` (Open Items), it is not
yet decided whether the contract models export JSON Schema directly or
whether schemas are derived from the typed models at build time. This
package exists so the location is fixed now, before Teams B/C wire up
imports against an alternate path.

When the decision lands, populate this package with one ``*.json`` per
public contract (``Diagnostic``, ``ResolvedLab``, ``OperationRun``,
``OperationEvent``, ``ResourceStatus``, ``Plan``, ``ApplyResult``,
``DestroyResult``, ``RetentionPolicy``, ``RetentionReport``).
"""
