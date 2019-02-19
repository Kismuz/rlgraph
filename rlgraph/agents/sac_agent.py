from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np

from rlgraph import get_backend
from rlgraph.agents import Agent
from rlgraph.utils import RLGraphError
from rlgraph.spaces import FloatBox, BoolBox, IntBox, Tuple
from rlgraph.components import Component, Synchronizable
from rlgraph.utils.decorators import rlgraph_api, graph_fn
from rlgraph.components import Memory, ContainerMerger, ContainerSplitter, PrioritizedReplay, LossFunction
from rlgraph.utils.util import strip_list
from rlgraph.utils.ops import flatten_op


if get_backend() == "tf":
    import tensorflow as tf
    from rlgraph.utils import tf_util
if get_backend() == "pytorch":
    import torch


class SACLossFunction(LossFunction):
    def __init__(self, target_entropy=None, discount=0.99, scope="sac-loss-function", **kwargs):
        super(SACLossFunction, self).__init__(discount=discount, scope=scope, **kwargs)
        self.target_entropy = target_entropy

    @rlgraph_api
    def loss(self, alpha, log_probs_next_sampled, q_values_next_sampled, q_values, log_probs_sampled,
             q_values_sampled, rewards, terminals):
        actor_loss_per_item, critic_loss_per_item, alpha_loss_per_item = self.loss_per_item(
            alpha, log_probs_next_sampled, q_values_next_sampled, q_values, log_probs_sampled,
            q_values_sampled, rewards, terminals
        )
        actor_loss = self.loss_average(actor_loss_per_item)
        critic_loss = self.loss_average(critic_loss_per_item)
        alpha_loss = self.loss_average(alpha_loss_per_item)
        return actor_loss, actor_loss_per_item, critic_loss, critic_loss_per_item, alpha_loss, alpha_loss_per_item

    @graph_fn
    def _graph_fn__critic_loss(self, alpha, log_probs_next_sampled, q_values_next_sampled, q_values, rewards, terminals):
        q_min_next = tf.reduce_min(tf.concat(q_values_next_sampled, axis=1), axis=1, keepdims=True)
        assert q_min_next.shape.as_list() == [None, 1]
        soft_state_value = q_min_next - alpha * log_probs_next_sampled
        q_target = rewards + self.discount * (1.0 - tf.cast(terminals, tf.float32)) * soft_state_value
        total_loss = 0.0
        for i, q_value in enumerate(q_values):
            loss = 0.5 * (q_value - tf.stop_gradient(q_target)) ** 2
            loss = tf.identity(loss, "critic_loss_per_item_{}".format(i + 1))
            total_loss += loss
        return total_loss

    @graph_fn
    def _graph_fn__actor_loss(self, alpha, log_probs_sampled, q_values_sampled):
        q_min = tf.reduce_min(tf.concat(q_values_sampled, axis=1), axis=1, keepdims=True)
        assert q_min.shape.as_list() == [None, 1]
        loss = alpha * log_probs_sampled - q_min
        loss = tf.identity(loss, "actor_loss_per_item")
        return loss

    @graph_fn
    def _graph_fn__alpha_loss(self, alpha, log_probs_sampled):
        loss = -alpha * tf.stop_gradient(log_probs_sampled + self.target_entropy)
        loss = tf.identity(loss, "alpha_loss_per_item")
        return loss

    @rlgraph_api
    def loss_per_item(self, alpha, log_probs_next_sampled, q_values_next_sampled, q_values, log_probs_sampled,
                      q_values_sampled, rewards, terminals):
        return self._graph_fn_loss_per_item(
            alpha, log_probs_next_sampled, q_values_next_sampled, q_values, log_probs_sampled, q_values_sampled,
            rewards, terminals
        )

    @graph_fn
    def _graph_fn_loss_per_item(self, alpha, log_probs_next_sampled, q_values_next_sampled, q_values, log_probs_sampled,
                                q_values_sampled, rewards, terminals):
        assert alpha.shape.as_list() == []
        assert log_probs_next_sampled.shape.as_list() == [None, 1]
        assert all(q.shape.as_list() == [None, 1] for q in q_values_next_sampled)
        assert all(q.shape.as_list() == [None, 1] for q in q_values)
        assert log_probs_sampled.shape.as_list() == [None, 1]
        assert all(q.shape.as_list() == [None, 1] for q in q_values_sampled)
        assert rewards.shape.as_list() == [None]
        assert terminals.shape.as_list() == [None]
        rewards = tf.expand_dims(rewards, axis=-1)
        terminals = tf.expand_dims(terminals, axis=-1)

        critic_loss_per_item = self._graph_fn__critic_loss(
            alpha=alpha,
            log_probs_next_sampled=log_probs_next_sampled,
            q_values_next_sampled=q_values_next_sampled,
            q_values=q_values,
            rewards=rewards,
            terminals=terminals
        )
        critic_loss_per_item = tf.squeeze(critic_loss_per_item, axis=1)

        actor_loss_per_item = self._graph_fn__actor_loss(
            alpha=alpha,
            log_probs_sampled=log_probs_sampled,
            q_values_sampled=q_values_sampled
        )
        actor_loss_per_item = tf.squeeze(actor_loss_per_item, axis=1)

        if self.target_entropy is not None:
            alpha_loss_per_item = self._graph_fn__alpha_loss(alpha=alpha, log_probs_sampled=log_probs_sampled)
            alpha_loss_per_item = tf.squeeze(alpha_loss_per_item, axis=1)
        else:
            # TODO: optimize this path
            alpha_loss_per_item = tf.zeros([tf.shape(rewards)[0]])

        return actor_loss_per_item, critic_loss_per_item, alpha_loss_per_item


class SyncSpecification(object):
    def __init__(self, sync_interval=None, sync_tau=None):
        self.sync_interval = sync_interval
        self.sync_tau = sync_tau


class SACAgentComponent(Component):
    def __init__(self, policy, q_function, preprocessor, memory, discount,
                 initial_alpha, target_entropy, optimizer, vf_optimizer, q_sync_spec, num_q_functions=2):
        super(SACAgentComponent, self).__init__(nesting_level=0)
        self._policy = policy
        self._preprocessor = preprocessor
        self._memory = memory
        self._q_functions = [q_function]
        self._q_functions += [q_function.copy(scope="{}-{}".format(q_function.scope, i + 1), trainable=True)
                              for i in range(num_q_functions - 1)]
        self._target_q_functions = [q.copy(scope="target-" + q.scope, trainable=True) for q in self._q_functions]
        for target_q in self._target_q_functions:
            if "synchronizable" not in target_q.sub_components:
                target_q.add_components(Synchronizable(), expose_apis="sync")
        self._optimizer = optimizer
        self.vf_optimizer = vf_optimizer
        self.initial_alpha = initial_alpha
        self.log_alpha = None
        self.target_entropy = target_entropy
        self.loss_function = SACLossFunction(target_entropy=target_entropy, discount=discount)

        memory_items = ["states", "actions", "rewards", "next_states", "terminals"]
        self._merger = ContainerMerger(*memory_items)
        self._splitter = ContainerSplitter(*memory_items)

        q_names = ["q_{}".format(i) for i in range(len(self._q_functions))]
        self._q_vars_merger = ContainerMerger(*q_names, scope="q_vars_merger")
        #self._q_vars_splitter = ContainerSplitter(*q_names, scope="q_vars_splitter")

        self.add_components(policy, preprocessor, memory, self._merger, self._splitter, self.loss_function,
                            optimizer, vf_optimizer, self._q_vars_merger)#, self._q_vars_splitter)
        self.add_components(*self._q_functions)
        self.add_components(*self._target_q_functions)

        self.steps_since_last_sync = None
        self.q_sync_spec = q_sync_spec

    def create_variables(self, input_spaces, action_space=None):
        self.steps_since_last_sync = self.get_variable("steps_since_last_sync", dtype="int", initializer=0)
        self.log_alpha = self.get_variable("log_alpha", dtype="float", initializer=np.log(self.initial_alpha))

    @rlgraph_api
    def get_policy_weights(self):
        # TODO: why is _variables() "protected"?
        return self._policy._variables()

    @rlgraph_api
    def get_q_weights(self):
        merged_weights = self._q_vars_merger.merge(*[q._variables() for q in self._q_functions])
        return merged_weights

    @rlgraph_api(must_be_complete=False)
    def set_policy_weights(self, weights):
        return self._policy.sync(weights)

    @rlgraph_api(must_be_complete=False)
    def set_q_weights(self, weights):
        #return self._value_function.sync(vf_weights)
        return None

    @rlgraph_api
    def preprocess_states(self, states):
        return self._preprocessor.preprocess(states)

    @rlgraph_api
    def insert_records(self, preprocessed_states, actions, rewards, next_states, terminals):
        records = self._merger.merge(preprocessed_states, actions, rewards, next_states, terminals)
        return self._memory.insert_records(records)

    @rlgraph_api
    def update_from_memory(self, batch_size):
        records, sample_indices, importance_weights = self._memory.get_records(batch_size)
        preprocessed_s, actions, rewards, preprocessed_s_prime, terminals = self._splitter.split(records)

        actor_step_op, critic_step_op, sync_op, alpha_step_op, actor_loss, actor_loss_per_item, critic_loss, critic_loss_per_item, alpha_loss, alpha_loss_per_item\
            = self.update_from_external_batch(
                preprocessed_s, actions, rewards, terminals, preprocessed_s_prime, importance_weights
            )

        ret = [actor_step_op, critic_step_op, sync_op, alpha_step_op, actor_loss, actor_loss_per_item, critic_loss, critic_loss_per_item, alpha_loss, alpha_loss_per_item]
        if isinstance(self._memory, PrioritizedReplay):
            update_pr_step_op = self._memory.update_records(sample_indices, critic_loss_per_item)
            ret.append(update_pr_step_op)

        return tuple(ret)

    @rlgraph_api
    def update_from_external_batch(self, preprocessed_states, actions, rewards, terminals,
                                             preprocessed_s_prime, importance_weights):
        actor_loss, actor_loss_per_item, critic_loss, critic_loss_per_item, alpha_loss, alpha_loss_per_item = \
            self.get_losses(preprocessed_states, actions, rewards, terminals, preprocessed_s_prime, importance_weights)

        policy_vars = self._policy._variables()
        q_vars = [q_func._variables() for q_func in self._q_functions]
        merged_q_vars = self._q_vars_merger.merge(*q_vars)
        critic_step_op, critic_loss, critic_loss_per_item = \
            self.vf_optimizer.step(merged_q_vars, critic_loss, critic_loss_per_item)

        actor_step_op, actor_loss, actor_loss_per_item = \
            self._optimizer.step(policy_vars, actor_loss, actor_loss_per_item)

        if self.target_entropy is not None:
            alpha_step_op = self._graph_fn__no_op()
            #alpha_step_op, alpha_loss, alpha_loss_per_item = self._optimizer.step(self.log_alpha, alpha_loss, alpha_loss_per_item)
        else:
            alpha_step_op = self._graph_fn__no_op()

        # TODO: optimizer for alpha

        sync_op = self.sync_targets()

        return actor_step_op, critic_step_op, sync_op, alpha_step_op, actor_loss, actor_loss_per_item, critic_loss, critic_loss_per_item,\
            alpha_loss, alpha_loss_per_item

    def _compute_q_values(self, q_functions, states, actions):
        flat_actions = flatten_op(actions)
        state_actions = [states]
        for flat_key, action_component in self._policy.action_space.flatten().items():
            if isinstance(action_component, IntBox):
                state_actions.append(self._graph_fn__one_hot(flat_actions[flat_key]))
            else:
                state_actions.append(flat_actions[flat_key])
        state_actions = self._graph_fn__concat(*state_actions)
        return tuple(q.value_output(state_actions) for q in q_functions)

    @rlgraph_api
    def get_q_values(self, preprocessed_states, actions):
        q_values = self._compute_q_values(
            self._q_functions, preprocessed_states, actions
        )
        return q_values

    @rlgraph_api
    def get_losses(self, preprocessed_states, actions, rewards, terminals, preprocessed_next_states, importance_weights):
        # TODO: internal states

        samples_next = self._policy.get_action_and_log_prob(preprocessed_next_states, deterministic=False)
        next_sampled_actions = samples_next["action"]
        log_probs_next_sampled = samples_next["log_prob"]

        q_values_next_sampled = self._compute_q_values(
            self._target_q_functions, preprocessed_next_states, next_sampled_actions
        )
        q_values = self._compute_q_values(self._q_functions, preprocessed_states, actions)
        samples = self._policy.get_action_and_log_prob(preprocessed_states, deterministic=False)
        sampled_actions = samples["action"]
        log_probs_sampled = samples["log_prob"]
        q_values_sampled = self._compute_q_values(self._q_functions, preprocessed_states, sampled_actions)

        alpha = self._graph_fn__compute_alpha()

        return self.loss_function.loss(
            alpha,
            log_probs_next_sampled,
            q_values_next_sampled,
            q_values,
            log_probs_sampled,
            q_values_sampled,
            rewards,
            terminals
        )

    @graph_fn
    def _graph_fn__compute_alpha(self):
        return tf.exp(self.log_alpha)

    @rlgraph_api
    def get_preprocessed_state_and_action(self, states, deterministic=False):
        preprocessed_states = self._preprocessor.preprocess(states)
        return self.action_from_preprocessed_state(preprocessed_states, deterministic)

    @rlgraph_api
    def action_from_preprocessed_state(self, preprocessed_states, deterministic=False):
        out = self._policy.get_action(preprocessed_states, deterministic=deterministic)
        return out["action"], preprocessed_states

    @rlgraph_api
    def sync_target_qnet(self):
        pass

    @graph_fn(returns=1)
    def _graph_fn__concat(self, *tensors):
        backend = get_backend()
        if backend == "tf":
            return tf.concat([tf_util.ensure_batched(t) for t in tensors], axis=1)
        elif backend == "pytorch":
            raise NotImplementedError("TODO: pytorch support")

    @graph_fn
    def _graph_fn__one_hot(self, tensor):
        backend = get_backend()
        if backend == "tf":
            return tf.one_hot(tensor, depth=5)
        elif backend == "pytorch":
            raise NotImplementedError("TODO: pytorch support")

    @rlgraph_api(requires_variable_completeness=True)
    def reset_targets(self):
        ops = []
        for q, target_q in zip(self._q_functions, self._target_q_functions):
            ops.append(target_q.sync(q._variables()))
        return tuple(ops)

    @rlgraph_api(requires_variable_completeness=True)
    def sync_targets(self):
        should_sync = self._graph_fn_get__should_sync()
        return self._graph_fn__sync(should_sync)

    @graph_fn(returns=1, requires_variable_completeness=True)
    def _graph_fn_get__should_sync(self):
        if get_backend() == "tf":
            inc_op = tf.assign_add(self.steps_since_last_sync, 1)
            should_sync = inc_op >= self.q_sync_spec.sync_interval

            def reset_op():
                op = tf.assign(self.steps_since_last_sync, 0)
                with tf.control_dependencies([op]):
                    return tf.no_op()

            sync_op = tf.cond(
                pred=inc_op >= self.q_sync_spec.sync_interval,
                true_fn=reset_op,
                false_fn=tf.no_op
            )
            with tf.control_dependencies([sync_op]):
                return tf.identity(should_sync)
        else:
            raise NotImplementedError("TODO")

    @graph_fn(returns=1, requires_variable_completeness=True)
    def _graph_fn__sync(self, should_sync):
        assign_ops = []
        tau = self.q_sync_spec.sync_tau
        if tau != 1.0:
            all_source_vars = [source.get_variables(collections=None, custom_scope_separator="-") for source in self._q_functions]
            all_dest_vars = [destination.get_variables(collections=None, custom_scope_separator="-") for destination in self._target_q_functions]
            for source_vars, dest_vars in zip(all_source_vars, all_dest_vars):
                for (source_key, source_var), (dest_key, dest_var) in zip(sorted(source_vars.items()), sorted(dest_vars.items())):
                    assign_ops.append(tf.assign(dest_var, tau * source_var + (1.0 - tau) * dest_var))
        else:
            all_source_vars = [source._variables() for source in self._q_functions]
            for source_vars, destination in zip(all_source_vars, self._target_q_functions):
                assign_ops.append(destination.sync(source_vars))
        assert len(assign_ops) > 0
        grouped_op = tf.group(assign_ops)

        def assign_op():
            # Make sure we are returning no_op as opposed to reference
            with tf.control_dependencies([grouped_op]):
                return tf.no_op()

        cond_assign_op = tf.cond(should_sync, true_fn=assign_op, false_fn=tf.no_op)
        with tf.control_dependencies([cond_assign_op]):
            return tf.no_op()

    @graph_fn
    def _graph_fn__no_op(self):
        return tf.no_op()

    @rlgraph_api
    def get_size(self):
        return self._memory.get_size()


class SACAgent(Agent):
    def __init__(self, double_q=True, initial_alpha=1.0, target_entropy=None, memory_spec=None, value_function_sync_spec=None, **kwargs):
        """
        This is an implementation of the Soft-Actor Critic algorithm.

        Paper: http://arxiv.org/abs/1801.01290

        Args:
            double_q (bool): Whether to train two q networks independently.
            initial_alpha (float): "The temperature parameter α determines the relative importance of the entropy term against the reward".
            memory_spec (Optional[dict,Memory]): The spec for the Memory to use for the DQN algorithm.
            update_spec (dict): Here we can have sync_interval or sync_tau (for the value network update).
        """
        super(SACAgent, self).__init__(
            policy_spec=dict(deterministic=False),
            name=kwargs.pop("name", "sac-agent"),
            **kwargs
        )

        self.double_q = double_q
        self.target_entropy = target_entropy
        self.initial_alpha = initial_alpha

        # Assert that the synch interval is a multiple of the update_interval.
        if "sync_interval" in self.update_spec:
            if self.update_spec["sync_interval"] / self.update_spec["update_interval"] != \
                    self.update_spec["sync_interval"] // self.update_spec["update_interval"]:
                raise RLGraphError(
                    "ERROR: sync_interval ({}) must be multiple of update_interval "
                    "({})!".format(self.update_spec["sync_interval"], self.update_spec["update_interval"])
                )
        elif "sync_tau" in self.update_spec:
            if self.update_spec["sync_tau"] <= 0 or self.update_spec["sync_tau"] > 1.0:
                raise RLGraphError(
                    "sync_tau ({}) must be in interval (0.0, 1.0]!".format(self.update_spec["sync_tau"])
                )
        else:
            self.update_spec["sync_tau"] = 0.005  # The value mentioned in the paper

        # Extend input Space definitions to this Agent's specific API-methods.
        preprocessed_state_space = self.preprocessed_state_space.with_batch_rank()
        reward_space = FloatBox(add_batch_rank=True)
        terminal_space = BoolBox(add_batch_rank=True)

        self.iterations = self.update_spec["num_iterations"]
        self.batch_size = self.update_spec["batch_size"]

        self.input_spaces.update(dict(
            actions=self.action_space.with_batch_rank(),
            preprocessed_states=preprocessed_state_space,
            rewards=reward_space,
            terminals=terminal_space,
            next_states=self.state_space.with_batch_rank(),
            preprocessed_next_states=preprocessed_state_space,
            states=self.state_space.with_batch_rank(add_batch_rank=True),
            batch_size=int,
            preprocessed_s_prime=self.state_space.with_batch_rank(add_batch_rank=True),
            importance_weights=FloatBox(add_batch_rank=True),
            deterministic=bool,
            weights="variables:{}".format(self.policy.scope)
        ))

        if value_function_sync_spec is None:
            value_function_sync_spec = SyncSpecification(
                sync_interval=self.update_spec["sync_interval"] // self.update_spec["update_interval"],
                sync_tau=self.update_spec["sync_tau"] if "sync_tau" in self.update_spec else 5e-3
            )

        self.memory = Memory.from_spec(memory_spec)
        print(memory_spec)
        self.root_component = SACAgentComponent(
            policy=self.policy,
            q_function=self.value_function,
            preprocessor=self.preprocessor,
            memory=self.memory,
            discount=self.discount,
            initial_alpha=self.initial_alpha,
            target_entropy=target_entropy,
            optimizer=self.optimizer,
            vf_optimizer=self.value_function_optimizer,
            q_sync_spec=value_function_sync_spec,
            num_q_functions=2 if self.double_q else 1
        )

        self.build_options = dict(vf_optimizer=self.value_function_optimizer)

        if self.auto_build:
            self._build_graph(
                [self.root_component], self.input_spaces, optimizer=self.optimizer,
                batch_size=self.update_spec["batch_size"],
                build_options=self.build_options
            )
            self.graph_built = True

    def define_graph_api(self, *args, **kwargs):
        pass

    def set_weights(self, policy_weights, value_function_weights=None):
        # TODO: Overrides parent but should this be policy of value function?
        return self.graph_executor.execute((self.root_component.set_policy_weights, policy_weights))

    def get_weights(self):
        return self.graph_executor.execute(self.root_component.get_policy_weights)

    def get_action(self, states, internals=None, use_exploration=True, apply_preprocessing=True, extra_returns=None):
        # TODO: common pattern - move to Agent
        """
        Args:
            extra_returns (Optional[Set[str],str]): Optional string or set of strings for additional return
                values (besides the actions). Possible values are:
                - 'preprocessed_states': The preprocessed states after passing the given states through the
                preprocessor stack.
                - 'internal_states': The internal states returned by the RNNs in the NN pipeline.
                - 'used_exploration': Whether epsilon- or noise-based exploration was used or not.

        Returns:
            tuple or single value depending on `extra_returns`:
                - action
                - the preprocessed states
        """
        extra_returns = {extra_returns} if isinstance(extra_returns, str) else (extra_returns or set())
        # States come in without preprocessing -> use state space.
        if apply_preprocessing:
            call_method = self.root_component.get_preprocessed_state_and_action
            batched_states = self.state_space.force_batch(states)
        else:
            call_method = self.root_component.action_from_preprocessed_state
            batched_states = states
        remove_batch_rank = batched_states.ndim == np.asarray(states).ndim + 1

        # Increase timesteps by the batch size (number of states in batch).
        batch_size = len(batched_states)
        self.timesteps += batch_size

        # Control, which return value to "pull" (depending on `additional_returns`).
        return_ops = [1, 0] if "preprocessed_states" in extra_returns else [1]
        ret = self.graph_executor.execute((
            call_method,
            [batched_states, not use_exploration],  # deterministic = not use_exploration
            # 0=preprocessed_states, 1=action
            return_ops
        ))
        if remove_batch_rank:
            return strip_list(ret)
        else:
            return ret

    def _observe_graph(self, preprocessed_states, actions, internals, rewards, next_states, terminals):
        self.graph_executor.execute((self.root_component.insert_records, [preprocessed_states, actions, rewards, next_states, terminals]))

    def update(self, batch=None):
        if batch is None:
            size = self.graph_executor.execute(self.root_component.get_size)
            # TODO: is this necessary?
            if size < self.batch_size:
                return 0.0, 0.0
            ret = self.graph_executor.execute((self.root_component.update_from_memory, [self.batch_size]))

            # Remove unnecessary return dicts (e.g. sync-op).
            if isinstance(ret, dict):
                ret = ret["update_from_memory"]
        else:
            # No sequence indices means terminals are used in place.
            batch_input = [batch["states"], batch["actions"], batch["rewards"], batch["terminals"], batch["next_states"]]
            ret = self.graph_executor.execute((self.root_component.update_from_external_batch, batch_input))

            # Remove unnecessary return dicts (e.g. sync-op).
            if isinstance(ret, dict):
                ret = ret["update_from_external_batch"]

        # [0] loss, [1] loss per item
        return ret[4], ret[5]

    def reset(self):
        """
        Resets our preprocessor, but only if it contains stateful PreprocessLayer Components (meaning
        the PreprocessorStack has at least one variable defined).
        """
        if self.preprocessing_required and len(self.preprocessor.variables) > 0:
            self.graph_executor.execute("reset_preprocessor")
        self.graph_executor.execute(self.root_component.reset_targets)

    def __repr__(self):
        return "SACAgent(double_q={}, initial_alpha={})".format(self.double_q, self.initial_alpha)
