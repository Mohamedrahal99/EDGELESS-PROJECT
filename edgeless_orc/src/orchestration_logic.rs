// SPDX-FileCopyrightText: © 2023 Claudio Cicconetti <c.cicconetti@iit.cnr.it>
// SPDX-FileCopyrightText: © 2023 Siemens AG
// SPDX-License-Identifier: MIT

use rand::Rng;
use rand::SeedableRng;
use rand::distributions::Distribution;

/// Keeps all the necessary state that is needed to make simple orchestration
/// decisions. Provides convenience methods that can be used by the
/// orchestrator.
pub struct OrchestrationLogic {
    /// Orchestration strategy.
    orchestration_strategy: crate::OrchestrationStrategy,
    /// Used by RoundRobin. Current index in vector nodes (circular).
    round_robin_current_index: usize,
    /// Random-number generator.
    rng: rand::rngs::StdRng,
    /// Vector of the nodes that can be selected.
    nodes: Vec<uuid::Uuid>,
    /// Capabilities of the nodes.
    capabilities: Vec<edgeless_api::node_registration::NodeCapabilities>,
    /// Resource providers of the nodes.
    resource_providers: Vec<std::collections::HashSet<String>>,
    /// Used by Random, pair of (weight, node_id).
    weights: Vec<f32>,
}

impl OrchestrationLogic {
    pub fn new(orchestration_strategy: crate::OrchestrationStrategy) -> Self {
        match orchestration_strategy {
            crate::OrchestrationStrategy::Random => log::info!("Orchestration logic strategy: random"),
            crate::OrchestrationStrategy::RoundRobin => log::info!("Orchestration logic strategy: round-robin"),
            crate::OrchestrationStrategy::RandomNode => {
                log::info!("Orchestration logic strategy: random-node (workflow co-location)")
            }
            crate::OrchestrationStrategy::Intelligent => {
                log::info!("Orchestration logic strategy: CAP (class-aware predictive placement)")
            }
            crate::OrchestrationStrategy::WeightedLeastLoaded => {
                log::info!("Orchestration logic strategy: weighted-least-loaded (class- and capacity-aware, no queue model)")
            }
            crate::OrchestrationStrategy::LeastLoaded => {
                log::info!("Orchestration logic strategy: least-loaded (agent count only, class- and capacity-blind)")
            }
            crate::OrchestrationStrategy::CapacityLeastLoaded => {
                log::info!("Orchestration logic strategy: capacity-least-loaded (agent count / measured capacity, class-blind)")
            }
        };

        Self {
            orchestration_strategy,
            round_robin_current_index: 0,
            rng: rand::rngs::StdRng::from_entropy(),
            nodes: vec![],
            capabilities: vec![],
            resource_providers: vec![],
            weights: vec![],
        }
    }

    pub fn update_nodes(
        &mut self,
        clients: &std::collections::HashMap<uuid::Uuid, crate::client_desc::ClientDesc>,
        resource_providers: &std::collections::HashMap<String, crate::resource_provider::ResourceProvider>,
    ) {
        // Refresh the nodes and weights data structures with the current set of nodes and their capabilities.
        self.nodes.clear();
        self.capabilities.clear();
        self.resource_providers.clear();
        self.weights.clear();
        for (node, desc) in clients {
            if desc.capabilities.do_not_use() || desc.cordoned {
                // Skip the node if it must not be used, no matter what.
                continue;
            }
            self.nodes.push(*node);
            self.capabilities.push(desc.capabilities.clone());
            self.resource_providers.push(
                resource_providers
                    .iter()
                    .filter(|(_, info)| info.node_id == *node)
                    .map(|(name, _)| name.clone())
                    .collect(),
            );
            let mut weight = (std::cmp::max(desc.capabilities.num_cores, desc.capabilities.num_cpus) as f32) * desc.capabilities.clock_freq_cpu;
            if weight == 0.0 {
                // Force a vanishing weight to an arbitrary value.
                weight = 1.0;
            }
            self.weights.push(weight);
        }
        assert!(self.nodes.len() == self.capabilities.len());
        assert!(self.nodes.len() == self.resource_providers.len());
        assert!(self.nodes.len() == self.weights.len());
        assert!(self.nodes.len() <= clients.len());
    }

    /// Filter only the nodes on which the given function can be deployed.
    pub fn feasible_nodes(&self, spawn_req: &edgeless_api::function_instance::SpawnFunctionRequest, nodes: &Vec<uuid::Uuid>) -> Vec<uuid::Uuid> {
        let mut candidates = vec![];

        for candidate in nodes {
            if let Some(ndx) = self.nodes.iter().position(|&x| x == *candidate)
                && OrchestrationLogic::is_node_feasible(
                    &spawn_req.spec.function_type,
                    &crate::deployment_requirements::DeploymentRequirements::from_annotations(&spawn_req.annotations),
                    &self.nodes[ndx],
                    &self.capabilities[ndx],
                    &self.resource_providers[ndx],
                )
            {
                candidates.push(self.nodes[ndx]);
            }
        }

        candidates
    }

    /// Return true if it is possible to assign a function requesting a given
    /// run-time and with given deployment requirements to a node with
    /// given UUID and capabilities.
    pub fn is_node_feasible(
        runtime: &str,
        reqs: &crate::deployment_requirements::DeploymentRequirements,
        node_id: &uuid::Uuid,
        capabilities: &edgeless_api::node_registration::NodeCapabilities,
        resource_providers: &std::collections::HashSet<String>,
    ) -> bool {
        capabilities.runtimes.contains(&runtime.to_string()) && reqs.is_feasible(node_id, capabilities, resource_providers)
    }

    /// Select the next node on which a function instance should be spawned,
    /// based on a general orchestration strategy as defined in the settings.
    /// Always match the deployment requirements specified with the nodes'
    /// capabilities.
    ///
    /// `same_workflow_nodes` are the nodes that already host another
    /// function/resource of the workflow this instance belongs to (empty if
    /// none, or if the workflow is unknown). Only RandomNode uses it.
    ///
    /// `sticky_nodes` is a HARD deployment constraint, honoured by every
    /// strategy: when it is `Some`, the instance may only be placed on one of
    /// those nodes. It carries the nodes that already host another component
    /// of the same sticky group (see the `sticky` annotation). If none of them
    /// is feasible the function returns `None`, which the orchestrator turns
    /// into a deployment error rather than placing the component elsewhere.
    pub fn next(
        &mut self,
        spawn_req: &edgeless_api::function_instance::SpawnFunctionRequest,
        same_workflow_nodes: &[uuid::Uuid],
        sticky_nodes: Option<&[uuid::Uuid]>,
    ) -> Option<uuid::Uuid> {
        if self.nodes.is_empty() {
            return None;
        }
        let reqs = crate::deployment_requirements::DeploymentRequirements::from_annotations(&spawn_req.annotations);

        // A sticky group that is already anchored restricts the admissible
        // nodes before any strategy runs. This is a constraint, not a
        // preference: if the intersection is empty we return None.
        if let Some(anchors) = sticky_nodes {
            let admissible: Vec<usize> = (0..self.nodes.len())
                .filter(|&i| {
                    anchors.contains(&self.nodes[i])
                        && Self::is_node_feasible(
                            &spawn_req.spec.function_type,
                            &reqs,
                            &self.nodes[i],
                            &self.capabilities[i],
                            &self.resource_providers[i],
                        )
                })
                .collect();
            if admissible.is_empty() {
                log::warn!(
                    "sticky constraint cannot be satisfied: none of the {} anchor node(s) can host this component",
                    anchors.len()
                );
                return None;
            }
            let ndx = admissible[self.rng.gen_range(0..admissible.len())];
            return Some(self.nodes[ndx]);
        }

        match self.orchestration_strategy {
            crate::OrchestrationStrategy::Random => {
                // Select only the nodes that are feasible.
                let mut candidates = vec![];
                let mut high: f32 = 0.0;
                for i in 0..self.nodes.len() {
                    if Self::is_node_feasible(
                        &spawn_req.spec.function_type,
                        &reqs,
                        &self.nodes[i],
                        &self.capabilities[i],
                        &self.resource_providers[i],
                    ) {
                        candidates.push((i, self.weights[i]));
                        high += self.weights[i];
                    }
                }
                if high > 0.0 {
                    let rv = rand::distributions::Uniform::new(0.0, high);
                    let rnd = rv.sample(&mut self.rng);
                    let mut sum = 0.0_f32;
                    for candidate in candidates {
                        sum += candidate.1;
                        if sum >= rnd {
                            return Some(self.nodes[candidate.0]);
                        }
                    }
                }
                None
            }
            crate::OrchestrationStrategy::RoundRobin => {
                // Prevent infinite loop: evaluate each node at most once.
                for _ in 0..self.nodes.len() {
                    // Wrap-around if the current index is out of bounds.
                    if self.round_robin_current_index >= self.nodes.len() {
                        self.round_robin_current_index = 0;
                    }

                    let cand_ndx = self.round_robin_current_index;
                    self.round_robin_current_index += 1;

                    if Self::is_node_feasible(
                        &spawn_req.spec.function_type,
                        &reqs,
                        &self.nodes[cand_ndx],
                        &self.capabilities[cand_ndx],
                        &self.resource_providers[cand_ndx],
                    ) {
                        return Some(self.nodes[cand_ndx]);
                    }
                }
                None
            }
            crate::OrchestrationStrategy::RandomNode => {
                // Workflow co-location:
                //  1. among the feasible nodes, prefer one that already hosts
                //     another function/resource of the same workflow;
                //  2. if none does (first instance of the workflow, or the
                //     workflow's nodes cannot host this one), pick uniformly at
                //     random among the feasible nodes.
                self.random_node(&spawn_req.spec.function_type, &reqs, same_workflow_nodes)
            }
            crate::OrchestrationStrategy::Intelligent
            | crate::OrchestrationStrategy::WeightedLeastLoaded
            | crate::OrchestrationStrategy::LeastLoaded
            | crate::OrchestrationStrategy::CapacityLeastLoaded => {
                // Agents (components declaring an agent class) are placed by
                // the policy in orchestrator_task.rs before this is reached.
                // Everything else - components that declare nothing - follows
                // its workflow when possible, as under RandomNode.
                self.random_node(&spawn_req.spec.function_type, &reqs, same_workflow_nodes)
            }
        }
    }

    pub fn is_random_node(&self) -> bool {
        matches!(self.orchestration_strategy, crate::OrchestrationStrategy::RandomNode)
    }

    /// True if the configured strategy is Intelligent (class-aware predictive placement).
    pub fn is_intelligent(&self) -> bool {
        matches!(self.orchestration_strategy, crate::OrchestrationStrategy::Intelligent)
    }

    /// True if the configured strategy is WeightedLeastLoaded, the
    /// class- and capacity-aware baseline without the queueing model.
    pub fn is_weighted_least_loaded(&self) -> bool {
        matches!(self.orchestration_strategy, crate::OrchestrationStrategy::WeightedLeastLoaded)
    }

    /// True if the configured strategy is CapacityLeastLoaded (CLL): smallest
    /// (agents + 1) / capacity, blind to class.
    pub fn is_capacity_least_loaded(&self) -> bool {
        matches!(self.orchestration_strategy, crate::OrchestrationStrategy::CapacityLeastLoaded)
    }

    /// True if the strategy places declared agents itself (any of the
    /// agent-level policies: CAP, WLL, CLL).
    pub fn is_agent_aware(&self) -> bool {
        self.is_intelligent() || self.is_weighted_least_loaded() || self.is_capacity_least_loaded()
    }

    /// Short policy name used in placement logs.
    pub fn policy_label(&self) -> &'static str {
        match self.orchestration_strategy {
            crate::OrchestrationStrategy::Intelligent => "intelligent",
            crate::OrchestrationStrategy::WeightedLeastLoaded => "wll",
            crate::OrchestrationStrategy::CapacityLeastLoaded => "cll",
            _ => "other",
        }
    }

    /// Select a node for a RESOURCE that belongs to an already-anchored sticky
    /// group. `provider_nodes` are the nodes offering the required provider.
    /// Returns `None` when no anchor node offers it, which the orchestrator
    /// turns into a deployment error.
    pub fn next_resource_sticky(&mut self, provider_nodes: &[uuid::Uuid], sticky_nodes: &[uuid::Uuid]) -> Option<uuid::Uuid> {
        let admissible: Vec<uuid::Uuid> = provider_nodes.iter().copied().filter(|n| sticky_nodes.contains(n)).collect();
        if admissible.is_empty() {
            log::warn!(
                "sticky constraint cannot be satisfied: none of the {} anchor node(s) offers the required resource provider",
                sticky_nodes.len()
            );
            return None;
        }
        Some(admissible[self.rng.gen_range(0..admissible.len())])
    }

    /// Select a node for a RESOURCE under the RandomNode strategy, given the
    /// nodes that offer the required provider. Same rule as for functions:
    /// prefer a node already hosting the workflow, else uniform random.
    /// Returns None if `provider_nodes` is empty.
    pub fn next_resource_random_node(&mut self, provider_nodes: &[uuid::Uuid], same_workflow_nodes: &[uuid::Uuid]) -> Option<uuid::Uuid> {
        if provider_nodes.is_empty() {
            return None;
        }
        let affine: Vec<uuid::Uuid> = provider_nodes.iter().copied().filter(|n| same_workflow_nodes.contains(n)).collect();
        let pool = if affine.is_empty() { provider_nodes } else { &affine[..] };
        let chosen = pool[self.rng.gen_range(0..pool.len())];
        log::debug!(
            "RandomNode resource placement: {} candidates, {} affine -> {}",
            provider_nodes.len(),
            affine.len(),
            chosen
        );
        Some(chosen)
    }

    /// RandomNode selection for functions (see `next`).
    fn random_node(
        &mut self,
        runtime: &str,
        reqs: &crate::deployment_requirements::DeploymentRequirements,
        same_workflow_nodes: &[uuid::Uuid],
    ) -> Option<uuid::Uuid> {
        let feasible: Vec<usize> = (0..self.nodes.len())
            .filter(|&i| Self::is_node_feasible(runtime, reqs, &self.nodes[i], &self.capabilities[i], &self.resource_providers[i]))
            .collect();
        if feasible.is_empty() {
            return None;
        }
        let affine: Vec<usize> = feasible.iter().copied().filter(|&i| same_workflow_nodes.contains(&self.nodes[i])).collect();
        let pool = if affine.is_empty() { &feasible } else { &affine };
        let ndx = pool[self.rng.gen_range(0..pool.len())];
        log::debug!(
            "RandomNode function placement: {} feasible, {} affine -> {}",
            feasible.len(),
            affine.len(),
            self.nodes[ndx]
        );
        Some(self.nodes[ndx])
    }
}

/// Tests
#[cfg(test)]
mod tests {

    use crate::affinity_level::AffinityLevel;
    use crate::deployment_requirements::DeploymentRequirements;

    #[test]
    fn test_orchestration_logic_is_node_feasible() {
        let node_id = uuid::Uuid::new_v4();
        let mut reqs = DeploymentRequirements::none();
        let mut caps = edgeless_api::node_registration::NodeCapabilities::minimum();
        let mut providers = std::collections::HashSet::new();
        let mut runtime = "RUST_WASM".to_string();

        // Empty requirements
        assert!(crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        // Match any node_id
        reqs.node_id_match_any.push(node_id);
        assert!(crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        reqs.node_id_match_any.push(uuid::Uuid::new_v4());
        assert!(crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        reqs.node_id_match_any.clear();
        reqs.node_id_match_any.push(uuid::Uuid::new_v4());
        assert!(!crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));
        reqs.node_id_match_any.clear();

        // Match all labels
        reqs.label_match_all.push("red".to_string());
        caps.labels.push("green".to_string());
        assert!(!crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        caps.labels.push("red".to_string());
        assert!(crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        reqs.label_match_all.push("blue".to_string());
        assert!(!crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        caps.labels.push("blue".to_string());
        assert!(crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        // Match all providers
        reqs.resource_match_all.push("file-1".to_string());
        assert!(!crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        providers.insert("file-1".to_string());
        assert!(crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        providers.insert("file-2".to_string());
        providers.insert("file-3".to_string());
        assert!(crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        reqs.resource_match_all.push("file-9".to_string());
        assert!(!crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        providers.insert("file-9".to_string());
        assert!(crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        // Match TEE and TPM
        reqs.tee = AffinityLevel::Required;
        assert!(!crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));
        caps.is_tee_running = true;
        assert!(crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        reqs.tpm = AffinityLevel::Required;
        assert!(!crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));
        caps.has_tpm = true;
        assert!(crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));

        // Match runtime
        runtime = "CONTAINER".to_string();
        assert!(!crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));
        runtime = "".to_string();
        assert!(!crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));
        runtime = "RUST_WASM".to_string();
        assert!(crate::orchestration_logic::OrchestrationLogic::is_node_feasible(
            &runtime, &reqs, &node_id, &caps, &providers
        ));
    }

    #[test]
    fn test_random_node_affinity() {
        let mut logic = crate::orchestration_logic::OrchestrationLogic::new(crate::OrchestrationStrategy::RandomNode);
        let a = uuid::Uuid::new_v4();
        let b = uuid::Uuid::new_v4();
        let c = uuid::Uuid::new_v4();
        let providers = vec![a, b, c];

        // no instance of the workflow yet: any provider node may be chosen
        let chosen = logic.next_resource_random_node(&providers, &[]).unwrap();
        assert!(providers.contains(&chosen));

        // the workflow already lives on b: b must be chosen, every time
        for _ in 0..20 {
            assert_eq!(logic.next_resource_random_node(&providers, &[b]).unwrap(), b);
        }

        // the workflow lives on a node that cannot host this resource: fall back to random
        let other = uuid::Uuid::new_v4();
        let chosen = logic.next_resource_random_node(&providers, &[other]).unwrap();
        assert!(providers.contains(&chosen));

        // no provider at all
        assert!(logic.next_resource_random_node(&[], &[b]).is_none());
    }
}