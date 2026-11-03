SHELL := /usr/bin/env bash

CLUSTER        ?= pulso
NAMESPACE      ?= pulso
INGRESS_HOST   ?= pulso.localtest.me
INGRESS_NGINX  ?= controller-v1.11.5
PYTHON         ?= python3
SVC            ?= orders-api
REPLICAS       ?= 3
SERVICES       := orders-api matching support-agents saga

export NAMESPACE

.DEFAULT_GOAL := help

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- checks (no cluster needed)

.PHONY: lint
lint: ## yamllint + shellcheck
	yamllint -s .
	shellcheck scripts/*.sh

.PHONY: test
test: ## Policy unit tests and manifest/compose consistency tests
	$(PYTHON) -m unittest discover -s tests -t . -v

.PHONY: prod-secrets-placeholder
prod-secrets-placeholder: ## Copy prod secret templates to *.env so the prod overlay renders (CI / local only)
	@for f in k8s/overlays/prod/secrets/*.env.example; do cp -n "$$f" "$${f%.example}" || true; done

.PHONY: render
render: ## kustomize build every overlay into rendered/
	./scripts/render.sh

.PHONY: validate
validate: render ## kubeconform + kube-linter + platform policy on rendered overlays
	PYTHON=$(PYTHON) ./scripts/validate.sh

.PHONY: compose-config
compose-config: ## Validate docker-compose.yml
	docker compose --env-file .env.example config --quiet

.PHONY: compose-up
compose-up: ## Build sibling repos and start the local stack
	docker compose up --build -d --wait

.PHONY: compose-down
compose-down: ## Stop the local stack (keeps volumes)
	docker compose down

# ---------------------------------------------------------------- kind

.PHONY: kind-up
kind-up: ## Create the kind cluster and install ingress-nginx
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --name $(CLUSTER) --config k8s/kind/cluster.yaml
	kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/$(INGRESS_NGINX)/deploy/static/provider/kind/deploy.yaml
	kubectl -n ingress-nginx wait --for=condition=ready pod -l app.kubernetes.io/component=controller --timeout=180s

.PHONY: kind-down
kind-down: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)

.PHONY: kind-load
kind-load: ## Build the sibling repos' images and load them into kind (tag :dev)
	@for svc in $(SERVICES); do \
	  docker build -t ghcr.io/pulso/pulso-$$svc:dev ../pulso-$$svc && \
	  kind load docker-image ghcr.io/pulso/pulso-$$svc:dev --name $(CLUSTER) || exit 1; \
	done
	docker build --target build -t ghcr.io/pulso/pulso-orders-api-migrate:dev ../pulso-orders-api
	kind load docker-image ghcr.io/pulso/pulso-orders-api-migrate:dev --name $(CLUSTER)

.PHONY: deploy-dev
deploy-dev: ## Apply overlays/dev (real images, see kind-load) and wait for rollout
	kubectl -n $(NAMESPACE) delete job orders-api-migrate --ignore-not-found
	kubectl apply -k k8s/overlays/dev
	./scripts/wait-ready.sh

.PHONY: deploy-smoke
deploy-smoke: ## Apply the CI overlay (dev + public placeholder images) and wait for rollout
	kubectl -n $(NAMESPACE) delete job orders-api-migrate --ignore-not-found
	kubectl apply -k k8s/ci/kind-smoke
	./scripts/wait-ready.sh

.PHONY: smoke
smoke: ## port-forward + curl /health on every service, then through the ingress
	INGRESS_HOST=$(INGRESS_HOST) ./scripts/smoke.sh

# ---------------------------------------------------------------- operations

.PHONY: status
status: ## Pods, HPAs and recent events
	kubectl -n $(NAMESPACE) get pods,hpa,pdb -o wide
	kubectl -n $(NAMESPACE) get events --sort-by=.lastTimestamp | tail -20

.PHONY: rollback
rollback: ## Roll back one Deployment to its previous ReplicaSet (SVC=orders-api)
	kubectl -n $(NAMESPACE) rollout undo deployment/$(SVC)
	kubectl -n $(NAMESPACE) rollout status deployment/$(SVC) --timeout=180s

.PHONY: scale
scale: ## Change an HPA's floor (SVC=matching REPLICAS=5); saga is scaled with kubectl scale
	kubectl -n $(NAMESPACE) patch hpa $(SVC) --type merge -p '{"spec":{"minReplicas":$(REPLICAS)}}'

.PHONY: clean
clean: ## Remove rendered manifests
	rm -rf rendered
