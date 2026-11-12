{{/*
Chart name, truncated to 63 chars.
*/}}
{{- define "s1gw.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name, truncated to 63 chars.
*/}}
{{- define "s1gw.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart name and version for the helm.sh/chart label.
*/}}
{{- define "s1gw.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "s1gw.selectorLabels" -}}
app.kubernetes.io/name: {{ include "s1gw.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "s1gw.labels" -}}
helm.sh/chart: {{ include "s1gw.chart" . }}
{{ include "s1gw.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
ServiceAccount name.
*/}}
{{- define "s1gw.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "s1gw.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image reference: repo@digest, else repo:tag, else repo:appVersion.
*/}}
{{- define "s1gw.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag | toString) -}}
{{- end -}}
{{- end }}

{{/*
Fail unless exactly one secret source is configured.
*/}}
{{- define "s1gw.validateSecretSource" -}}
{{- $sources := list -}}
{{- if .Values.externalSecret.enabled -}}
{{- $sources = append $sources "externalSecret.enabled" -}}
{{- end -}}
{{- if .Values.existingSecret -}}
{{- $sources = append $sources "existingSecret" -}}
{{- end -}}
{{- if .Values.secret.create -}}
{{- $sources = append $sources "secret.create" -}}
{{- end -}}
{{- if ne (len $sources) 1 -}}
{{- fail (printf "exactly one secret source must be set: externalSecret.enabled, existingSecret, or secret.create (found %d: [%s])" (len $sources) (join ", " $sources)) -}}
{{- end -}}
{{- end }}

{{/*
Name of the Secret the pod reads from. Validates the secret source first.
*/}}
{{- define "s1gw.secretName" -}}
{{- $_ := include "s1gw.validateSecretSource" . -}}
{{- if .Values.externalSecret.enabled -}}
{{- default (include "s1gw.fullname" .) .Values.externalSecret.target.name -}}
{{- else if .Values.existingSecret -}}
{{- .Values.existingSecret -}}
{{- else -}}
{{- include "s1gw.fullname" . -}}
{{- end -}}
{{- end }}

{{/*
Non-secret env for the ConfigMap. Also hashed into checksum/config, so the
Deployment does not need to include another template file.
*/}}
{{- define "s1gw.configData" -}}
PORT: {{ .Values.service.port | int64 | quote }}
METRICS_PORT: {{ .Values.service.metricsPort | int64 | quote }}
GEMINI_MODEL: {{ .Values.gemini.model | quote }}
GEMINI_BASE_URL: {{ .Values.gemini.baseUrl | quote }}
{{- with .Values.gemini.imageResolution }}
GEMINI_IMAGE_RESOLUTION: {{ . | quote }}
{{- end }}
UPSTREAM_TIMEOUT_SECONDS: {{ .Values.config.upstreamTimeoutSeconds | int64 | quote }}
UPSTREAM_MAX_RETRIES: {{ .Values.config.upstreamMaxRetries | int64 | quote }}
MAX_BODY_BYTES: {{ .Values.config.maxBodyBytes | int64 | quote }}
ADVERTISED_MODELS: {{ .Values.config.advertisedModels | quote }}
LOG_LEVEL: {{ .Values.config.logLevel | quote }}
LOG_FORMAT: {{ .Values.config.logFormat | quote }}
OTEL_SERVICE_NAME: {{ .Chart.Name | quote }}
OTEL_METRICS_EXPORTER: {{ .Values.otel.metricsExporter | quote }}
{{- with .Values.otel.otlpEndpoint }}
OTEL_EXPORTER_OTLP_ENDPOINT: {{ . | quote }}
{{- end }}
OTEL_EXPORTER_OTLP_PROTOCOL: {{ .Values.otel.otlpProtocol | quote }}
{{- end }}

{{/*
stringData for the chart-managed Secret (secret.create=true). Also hashed into checksum/secret.
*/}}
{{- define "s1gw.secretData" -}}
{{ .Values.secretKeys.geminiApiKey }}: {{ required "secret.geminiApiKey is required when secret.create=true" .Values.secret.geminiApiKey | quote }}
{{- with .Values.secret.inboundApiKeys }}
{{ $.Values.secretKeys.inboundApiKeys }}: {{ . | quote }}
{{- end }}
{{- end }}
