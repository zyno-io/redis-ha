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
{{- define "redis.mounts" -}}
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
- name: redis-data
  mountPath: /data
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
  value: {{ printf "/secrets/%s" .Values.auth.existingSecretPasswordKey | quote }}
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
- name: REDIS_BOOTSTRAP
  value: {{ ternary "yes" "no" .Values.bootstrap | quote }}
- name: REDIS_MIGRATE_SENTINEL_IDENTITY
  value: {{ ternary "yes" "no" .Values.migration.preserveLegacySentinelIdentity | quote }}
{{- end -}}

{{- define "redis.lifecycleName" -}}
{{- $content := printf "%s\n%s" (.Files.Get "files/startup.sh") (.Files.Get "files/shutdown.sh") -}}
{{- printf "%s-lifecycle-%s" .Release.Name ($content | sha256sum | trunc 12) -}}
{{- end -}}
