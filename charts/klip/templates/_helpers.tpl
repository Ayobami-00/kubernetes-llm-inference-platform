{{- define "klip.name" -}}{{ .Release.Name }}{{- end -}}
{{- define "klip.artifacts" -}}{{ default (printf "%s-artifacts" .Release.Name) .Values.artifacts.existingClaim }}{{- end -}}
{{- define "klip.env" -}}
- name: MANAGEMENT_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ required "database.existingSecret is required" .Values.database.existingSecret }}
      key: {{ .Values.database.urlKey }}
- name: MANAGEMENT_DATABASE_REQUIRE_TLS
  value: {{ .Values.database.requireTLS | quote }}
{{- if .Values.database.caSecret }}
- name: MANAGEMENT_DATABASE_CA_FILE
  value: /run/secrets/database/ca.crt
{{- end }}
- name: MANAGEMENT_ADMIN_PASSWORD_FILE
  value: /run/secrets/management/admin-password
- name: MANAGEMENT_LAMBDA_API_KEY_FILE
  value: /run/secrets/management/lambda-api-key
- name: MANAGEMENT_ARTIFACTS_ROOT
  value: /artifacts
- name: MANAGEMENT_ALLOWED_ORIGIN
  value: {{ required "publicOrigin is required" .Values.publicOrigin | quote }}
- name: MANAGEMENT_SECURE_COOKIES
  value: {{ not .Values.localDevelopment | quote }}
- name: MANAGEMENT_RUNNER_NAMESPACE
  value: {{ .Release.Namespace }}
- name: MANAGEMENT_RUNNER_SERVICE_ACCOUNT
  value: {{ .Release.Name }}-runner
- name: MANAGEMENT_SERVING_NAMESPACE
  value: {{ .Values.runner.servingNamespace | quote }}
- name: MANAGEMENT_ARTIFACT_CLAIM
  value: {{ include "klip.artifacts" . }}
- name: MANAGEMENT_SIMULATION_RUNNER
  value: {{ .Values.runner.simulationAdapter | quote }}
- name: MANAGEMENT_REAL_RUNNER
  value: {{ .Values.runner.realAdapter | quote }}
- name: MANAGEMENT_RUNNER_IMAGE
  value: {{ .Values.runner.image | quote }}
- name: MANAGEMENT_RUNNER_SECRET_REFS
  value: {{ .Values.runner.secretRefs | toJson | quote }}
- name: MANAGEMENT_RUNNER_COMMAND
  value: {{ .Values.runner.command | toJson | quote }}
- name: MANAGEMENT_RUNNER_RESOURCES
  value: {{ .Values.runner.resources | toJson | quote }}
- name: MANAGEMENT_RUNNER_NODE_SELECTOR
  value: {{ .Values.runner.nodeSelector | toJson | quote }}
- name: MANAGEMENT_RUNNER_TOLERATIONS
  value: {{ .Values.runner.tolerations | toJson | quote }}
{{- end -}}
{{- define "klip.mounts" -}}
- {name: management-secrets, mountPath: /run/secrets/management, readOnly: true}
- {name: artifacts, mountPath: /artifacts}
- {name: tmp, mountPath: /tmp}
{{- if .Values.database.caSecret }}
- {name: database-ca, mountPath: /run/secrets/database, readOnly: true}
{{- end }}
{{- end -}}
{{- define "klip.volumes" -}}
- name: management-secrets
  secret:
    secretName: {{ required "auth.existingSecret is required" .Values.auth.existingSecret }}
    items:
      - {key: {{ .Values.auth.adminPasswordKey }}, path: admin-password}
      - {key: {{ .Values.auth.lambdaApiKeyKey }}, path: lambda-api-key}
- name: artifacts
  persistentVolumeClaim:
    claimName: {{ include "klip.artifacts" . }}
- name: tmp
  emptyDir: {}
{{- if .Values.database.caSecret }}
- name: database-ca
  secret:
    secretName: {{ .Values.database.caSecret }}
    items: [{key: {{ .Values.database.caKey }}, path: ca.crt}]
{{- end }}
{{- end -}}
{{- define "klip.availability" -}}
{{- $root := .root -}}
{{- $role := .role -}}
{{- if $root.Values.availability.enabled }}
{{- if lt (int (index $root.Values.replicas $role)) 2 -}}
{{- fail (printf "HA requires at least two %s replicas" $role) -}}
{{- end }}
affinity:
  podAntiAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      - topologyKey: kubernetes.io/hostname
        labelSelector:
          matchLabels:
            app.kubernetes.io/name: {{ $root.Release.Name }}
            app.kubernetes.io/component: {{ $role }}
{{- if $root.Values.availability.spreadAcrossZones }}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app.kubernetes.io/name: {{ $root.Release.Name }}
        app.kubernetes.io/component: {{ $role }}
{{- end }}
{{- end }}
{{- end -}}
