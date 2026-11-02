{{- define "redis.selector" -}}
app.kubernetes.io/name: redis
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: node
{{- end -}}
{{- define "redis.security" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: [ALL]
readOnlyRootFilesystem: true
runAsUser: 1001
runAsGroup: 1001
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}
{{- define "redis.healthInit" -}}
- name: install-health
  image: {{ .Values.health.image | quote }}
  imagePullPolicy: IfNotPresent
  args: [install, /health-bin]
  resources: {{ toYaml .Values.health.resources | nindent 4 }}
  securityContext: {{ include "redis.security" . | nindent 4 }}
  volumeMounts:
    - name: health-bin
      mountPath: /health-bin
{{- end -}}
{{- define "redis.mounts" -}}
- name: health-bin
  mountPath: /health-bin
  readOnly: true
- name: scripts
  mountPath: /shutdown
  readOnly: true
- name: configuration
  mountPath: /config
  readOnly: true
- name: credentials
  mountPath: /secrets
  readOnly: true
- name: runtime
  mountPath: /run/redis-config
- name: shutdown-state
  mountPath: /run/redis-shutdown
- name: tmp
  mountPath: /tmp
- name: bootstrap
  mountPath: /bootstrap
  readOnly: true
- name: redis-data
  mountPath: /data
{{ include "redis.roleMounts" . }}
{{- end -}}
{{- define "redis.tlsServerName" -}}
{{- default (printf "%s.%s.svc.%s" .Release.Name .Release.Namespace .Values.clusterDomain) .Values.tls.serverName -}}
{{- end -}}
{{- define "redis.metricsTlsMounts" -}}
{{- if .Values.tls.enabled }}
- name: metrics-ca
  mountPath: /redis-ca
  readOnly: true
{{- if .Values.metrics.tlsSecret }}
- name: metrics-client-tls
  mountPath: /client-tls
  readOnly: true
{{- end }}
{{- end }}
{{- end -}}
{{- define "redis.metricsTlsVolumes" -}}
{{- if .Values.tls.enabled }}
- name: metrics-ca
  secret:
    secretName: {{ .Values.tls.existingSecret | quote }}
    items: [{key: {{ .Values.tls.caKey | quote }}, path: ca.crt}]
{{- if .Values.metrics.tlsSecret }}
- name: metrics-client-tls
  secret:
    secretName: {{ .Values.metrics.tlsSecret | quote }}
    items: [{key: tls.crt, path: tls.crt}, {key: tls.key, path: tls.key}]
{{- end }}
{{- end }}
{{- end -}}
{{- define "redis.rolePasswordFile" -}}
{{- $root := index . 0 -}}{{- $role := index . 1 -}}
{{- $settings := index $root.Values.auth $role -}}
{{- if $settings.existingSecret -}}/role-credentials/{{ $role }}/password
{{- else -}}/secrets/{{ default $root.Values.auth.existingSecretPasswordKey $settings.passwordKey }}
{{- end -}}
{{- end -}}
{{- define "redis.roleMounts" -}}
{{- range $role := list "operator" "replication" "monitor" "discovery" }}
{{- if (index $.Values.auth $role).existingSecret }}
- name: {{ $role }}-credential
  mountPath: /role-credentials/{{ $role }}
  readOnly: true
{{- end }}
{{- end }}
{{- if and .Values.auth.acl.existingSecret .Values.metrics.sentinel.enabled }}
- name: sentinel-metrics-credential
  mountPath: /role-credentials/sentinel-metrics
  readOnly: true
{{- end }}
{{- if .Values.tls.enabled }}
- name: tls
  mountPath: /tls
  readOnly: true
{{- end }}
{{- if .Values.auth.acl.existingSecret }}
- name: external-acl
  mountPath: /external-acl
  readOnly: true
{{- end }}
{{- end -}}
{{- define "redis.roleVolumes" -}}
{{- range $role := list "operator" "replication" "monitor" "discovery" }}
{{- $settings := index $.Values.auth $role }}
{{- if $settings.existingSecret }}
- name: {{ $role }}-credential
  secret:
    secretName: {{ $settings.existingSecret | quote }}
    defaultMode: 0440
    items: [{key: {{ default $.Values.auth.existingSecretPasswordKey $settings.passwordKey | quote }}, path: password}]
{{- end }}
{{- end }}
{{- if and .Values.auth.acl.existingSecret .Values.metrics.sentinel.enabled }}
- name: sentinel-metrics-credential
  secret:
    secretName: {{ default (default .Values.auth.existingSecret .Values.metrics.existingSecret) .Values.metrics.sentinel.existingSecret | quote }}
    defaultMode: 0440
    items: [{key: {{ default (default .Values.auth.existingSecretPasswordKey .Values.metrics.passwordKey) .Values.metrics.sentinel.passwordKey | quote }}, path: password}]
{{- end }}
{{- if .Values.tls.enabled }}
- name: tls
  secret:
    secretName: {{ required "tls.existingSecret is required when TLS is enabled" .Values.tls.existingSecret | quote }}
    defaultMode: 0440
    items:
      - {key: {{ .Values.tls.certKey | quote }}, path: tls.crt}
      - {key: {{ .Values.tls.keyKey | quote }}, path: tls.key}
      - {key: {{ .Values.tls.caKey | quote }}, path: ca.crt}
{{- end }}
{{- if .Values.auth.acl.existingSecret }}
- name: external-acl
  secret:
    secretName: {{ .Values.auth.acl.existingSecret | quote }}
    defaultMode: 0440
    items: [{key: {{ .Values.auth.acl.existingSecretKey | quote }}, path: users.acl}]
{{- end }}
{{- end -}}
{{- define "redis.env" -}}
# Override Kubernetes Service-link REDIS_PORT=tcp://... with CLI port numbers.
- name: REDIS_PORT
  value: '6379'
- name: REDIS_SENTINEL_PORT
  value: '26379'
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: POD_IP
  valueFrom:
    fieldRef:
      fieldPath: status.podIP
- name: REDIS_PASSWORD_FILE
  value: {{ include "redis.rolePasswordFile" (list . "operator") | quote }}
- name: REDIS_USERNAME
  value: {{ .Values.auth.operator.username | quote }}
{{- range $role := list "replication" "monitor" "discovery" }}
- name: REDIS_{{ upper $role }}_USERNAME
  value: {{ (index $.Values.auth $role).username | quote }}
- name: REDIS_{{ upper $role }}_PASSWORD_FILE
  value: {{ include "redis.rolePasswordFile" (list $ $role) | quote }}
{{- end }}
- name: REDIS_HEALTH_REQUIRE_QUORUM
  value: {{ ternary "yes" "no" .Values.health.requireQuorum | quote }}
- name: REDIS_HEALTH_MIN_REPLICAS
  value: {{ .Values.health.minReplicas | quote }}
- name: REDIS_PERSISTENCE_ENABLED
  value: {{ ternary "yes" "no" .Values.replica.persistence.enabled | quote }}
{{- if .Values.tls.enabled }}
- {name: REDIS_TLS_ENABLED, value: "yes"}
- {name: REDIS_TLS_CERT_FILE, value: /tls/tls.crt}
- {name: REDIS_TLS_KEY_FILE, value: /tls/tls.key}
- {name: REDIS_TLS_CA_FILE, value: /tls/ca.crt}
- {name: REDIS_TLS_SERVER_NAME, value: {{ include "redis.tlsServerName" . | quote }}}
{{- end }}
{{- if .Values.auth.acl.existingSecret }}
- {name: REDIS_EXTERNAL_ACL_FILE, value: /external-acl/users.acl}
{{- if .Values.metrics.sentinel.enabled }}
- {name: REDIS_SENTINEL_METRICS_USERNAME, value: {{ .Values.metrics.sentinel.username | quote }}}
- {name: REDIS_SENTINEL_METRICS_PASSWORD_FILE, value: /role-credentials/sentinel-metrics/password}
{{- end }}
{{- end }}
- name: REDIS_SHUTDOWN_MASTER_SET
  value: {{ .Values.sentinel.masterSet | quote }}
- name: REDIS_SHUTDOWN_REPLICAS
  value: {{ .Values.replica.replicaCount | quote }}
- name: REDIS_SENTINEL_QUORUM
  value: {{ .Values.sentinel.quorum | quote }}
- name: REDIS_NODE_PREFIX
  value: {{ printf "%s-node" .Release.Name | quote }}
- name: REDIS_HEADLESS
  value: {{ printf "%s-headless.%s.svc.%s" .Release.Name .Release.Namespace .Values.clusterDomain | quote }}
- name: REDIS_BOOTSTRAP_FILE
  value: /bootstrap/enabled
- name: REDIS_MIGRATE_SENTINEL_IDENTITY
  value: {{ ternary "yes" "no" .Values.migration.preserveLegacySentinelIdentity | quote }}
{{- end -}}

{{- define "redis.lifecycleName" -}}
{{- $content := printf "%s\n%s\n%s" (.Files.Get "files/startup.sh") (.Files.Get "files/shutdown.sh") (.Files.Get "files/transport.sh") -}}
{{- printf "%s-lifecycle-%s" .Release.Name ($content | sha256sum | trunc 12) -}}
{{- end -}}
