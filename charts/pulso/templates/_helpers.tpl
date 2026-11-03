{{/* Common labels. Usage: include "pulso.labels" (list $ "orders-api") */}}
{{- define "pulso.labels" -}}
{{- $root := index . 0 -}}
{{- $name := index . 1 -}}
app.kubernetes.io/name: {{ $name }}
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/part-of: pulso
app.kubernetes.io/managed-by: {{ $root.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" $root.Chart.Name $root.Chart.Version | replace "+" "_" }}
{{- end -}}

{{/* Selector labels. Usage: include "pulso.selectorLabels" (list $ "orders-api") */}}
{{- define "pulso.selectorLabels" -}}
{{- $root := index . 0 -}}
{{- $name := index . 1 -}}
app.kubernetes.io/name: {{ $name }}
app.kubernetes.io/instance: {{ $root.Release.Name }}
{{- end -}}

{{/* Image reference. Usage: include "pulso.image" (list $ "pulso-orders-api" "1.4.2") */}}
{{- define "pulso.image" -}}
{{- $root := index . 0 -}}
{{- printf "%s/%s:%s" $root.Values.image.registry (index . 1) (default $root.Values.image.tag (index . 2)) -}}
{{- end -}}

{{/* Egress rule list for a data store: in-cluster pods or the managed CIDR.
     Usage: include "pulso.dataEgressPeer" (list $ "postgres") */}}
{{- define "pulso.dataEgressPeer" -}}
{{- $root := index . 0 -}}
{{- $store := index . 1 -}}
{{- if $root.Values.dataStores.inCluster }}
- podSelector:
    matchLabels:
      app.kubernetes.io/name: {{ $store }}
{{- else }}
- ipBlock:
    cidr: {{ index $root.Values.dataStores (printf "%sCidr" $store) }}
{{- end }}
{{- end -}}
