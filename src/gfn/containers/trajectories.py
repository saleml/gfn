from __future__ import annotations

from typing import Sequence

import torch

from gfn.actions import Actions
from gfn.containers.base import Container
from gfn.containers.state_pairs import StatePairs
from gfn.containers.transitions import Transitions
from gfn.env import Env
from gfn.states import DiscreteStates, States
from gfn.utils.common import has_log_probs


# TODO: remove env from this class?
class Trajectories(Container):
    """Container for complete trajectories (starting in $s_0$ and ending in $s_f$).

    Trajectories are represented as a States object with bi-dimensional batch shape.
    Actions are represented as an Actions object with bi-dimensional batch shape.
    The first dimension represents the time step, the second dimension represents
    the trajectory index. Because different trajectories may have different lengths,
    shorter trajectories are padded with the tensor representation of the terminal
    state ($s_f$ or $s_0$ depending on the direction of the trajectory), and
    actions is appended with dummy actions. The `when_is_done` tensor represents
    the time step at which each trajectory ends.

    Attributes:
        env: The environment in which the trajectories are defined.
        states: The states of the trajectories.
        actions: The actions of the trajectories.
        when_is_done: Tensor of shape (n_trajectories,) indicating the time step at which each trajectory ends.
        is_backward: Whether the trajectories are backward or forward.
        log_rewards: Tensor of shape (n_trajectories,) containing the log rewards of the trajectories.
        log_probs: Tensor of shape (max_length, n_trajectories) indicating the log probabilities of the
            trajectories' actions.

    """

    def __init__(
        self,
        env: Env,
        states: States | None = None,
        conditioning: torch.Tensor | None = None,
        actions: Actions | None = None,
        when_is_done: torch.Tensor | None = None,
        is_backward: bool = False,
        log_rewards: torch.Tensor | None = None,
        log_probs: torch.Tensor | None = None,
        estimator_outputs: torch.Tensor | None = None,
    ) -> None:
        """
        Args:
            env: The environment in which the trajectories are defined.
            states: The states of the trajectories.
            actions: The actions of the trajectories.
            when_is_done: Tensor of shape (n_trajectories,) indicating the time step at which each trajectory ends.
            is_backward: Whether the trajectories are backward or forward.
            log_rewards: Tensor of shape (n_trajectories,) containing the log rewards of the trajectories.
            log_probs: Tensor of shape (max_length, n_trajectories) indicating the log probabilities of
                the trajectories' actions.
            estimator_outputs: Tensor of shape (batch_shape, output_dim).
                When forward sampling off-policy for an n-step trajectory,
                n forward passes will be made on some function approximator,
                which may need to be re-used (for example, for evaluating PF). To avoid
                duplicated effort, the outputs of the forward passes can be stored here.

        If states is None, then the states are initialized to an empty States object,
        that can be populated on the fly. If log_rewards is None, then `env.log_reward`
        is used to compute the rewards, at each call of self.log_rewards
        """
        self.env = env
        self.conditioning = conditioning
        self.is_backward = is_backward
        self.states = (
            states if states is not None else env.states_from_batch_shape((0, 0))
        )
        assert len(self.states.batch_shape) == 2
        self.actions = (
            actions if actions is not None else env.actions_from_batch_shape((0, 0))
        )
        assert len(self.actions.batch_shape) == 2
        self.when_is_done = (
            when_is_done
            if when_is_done is not None
            else torch.full(size=(0,), fill_value=-1, dtype=torch.long)
        )
        assert (
            self.when_is_done.shape == (self.n_trajectories,)
            and self.when_is_done.dtype == torch.long
        )

        self._log_rewards = (
            log_rewards
            if log_rewards is not None
            else torch.full(size=(0,), fill_value=0, dtype=torch.float)
        )
        assert (
            self._log_rewards.shape == (self.n_trajectories,)
            and self._log_rewards.dtype == torch.float
        )

        if log_probs is not None and log_probs.shape != (0, 0):
            assert (
                log_probs.shape == (self.max_length, self.n_trajectories)
                and log_probs.dtype == torch.float
            ), f"log_probs.shape={log_probs.shape}, "
            f"self.max_length={self.max_length}, "
            f"self.n_trajectories={self.n_trajectories}"
        else:
            log_probs = torch.full(size=(0, 0), fill_value=0, dtype=torch.float)
        self.log_probs: torch.Tensor = log_probs

        self.estimator_outputs = estimator_outputs
        if self.estimator_outputs is not None:
            #  TODO: check why this fails.
            # assert self.estimator_outputs.shape[:len(self.states.batch_shape)] == self.states.batch_shape
            assert self.estimator_outputs.dtype == torch.float

    def __repr__(self) -> str:
        states = self.states.tensor.transpose(0, 1)
        assert states.ndim == 3
        trajectories_representation = ""
        assert isinstance(
            self.env.s0, torch.Tensor
        ), "not supported for Graph trajectories."
        assert isinstance(
            self.env.sf, torch.Tensor
        ), "not supported for Graph trajectories."

        for traj in states[:10]:
            one_traj_repr = []
            for step in traj:
                one_traj_repr.append(str(step.cpu().numpy()))
                if self.is_backward and step.equal(self.env.s0):
                    break
                elif not self.is_backward and step.equal(self.env.sf):
                    break
            trajectories_representation += "-> ".join(one_traj_repr) + "\n"
        return (
            f"Trajectories(n_trajectories={self.n_trajectories}, max_length={self.max_length}, First 10 trajectories:"
            + f"states=\n{trajectories_representation}"
            # + f"actions=\n{self.actions.tensor.squeeze().transpose(0, 1)[:10].numpy()}, "
            + f"when_is_done={self.when_is_done[:10].cpu().numpy()})"
        )

    @property
    def n_trajectories(self) -> int:
        return self.states.batch_shape[1]

    def __len__(self) -> int:
        return self.n_trajectories

    @property
    def max_length(self) -> int:
        if len(self) == 0:
            return 0

        return self.actions.batch_shape[0]

    @property
    def last_states(self) -> States:
        return self.states[self.when_is_done - 1, torch.arange(self.n_trajectories)]

    @property
    def log_rewards(self) -> torch.Tensor | None:
        """Returns the log rewards of the trajectories as a tensor of shape (n_trajectories,)."""
        if self._log_rewards is not None:
            assert self._log_rewards.shape == (self.n_trajectories,)
            return self._log_rewards
        if self.is_backward:
            return None
        try:
            return self.env.log_reward(self.last_states)
        except NotImplementedError:
            return torch.log(self.env.reward(self.last_states))

    def __getitem__(
        self, index: int | slice | tuple | Sequence[int] | Sequence[bool] | torch.Tensor
    ) -> Trajectories:
        """Returns a subset of the `n_trajectories` trajectories."""
        if isinstance(index, int):
            index = [index]
        when_is_done = self.when_is_done[index]
        new_max_length = when_is_done.max().item() if len(when_is_done) > 0 else 0
        states = self.states[:, index]
        actions = self.actions[:, index]
        states = states[: 1 + new_max_length]
        actions = actions[:new_max_length]
        if self.log_probs.shape != (0, 0):
            log_probs = self.log_probs[:, index]
            log_probs = log_probs[:new_max_length]
        else:
            log_probs = self.log_probs
        log_rewards = self._log_rewards[index] if self._log_rewards is not None else None
        if self.estimator_outputs is not None:
            # TODO: Is there a safer way to index self.estimator_outputs for
            #       for n-dimensional estimator outputs?
            #
            # First we index along the first dimension of the estimator outputs.
            # This can be thought of as the instance dimension, and is
            # compatible with all supported indexing approaches (dim=1).
            # All dims > 1 are not explicitly indexed unless the dimensionality
            # of `index` matches all dimensions of `estimator_outputs` aside
            # from the first (trajectory) dimension.
            estimator_outputs = self.estimator_outputs[:, index]
            # Next we index along the trajectory length (dim=0)
            estimator_outputs = estimator_outputs[:new_max_length]
        else:
            estimator_outputs = None

        return Trajectories(
            env=self.env,
            states=states,
            actions=actions,
            when_is_done=when_is_done,
            is_backward=self.is_backward,
            log_rewards=log_rewards,
            log_probs=log_probs,
            estimator_outputs=estimator_outputs,
        )

    @staticmethod
    def extend_log_probs(log_probs: torch.Tensor, new_max_length: int) -> torch.Tensor:
        """Extend the log_probs matrix by adding 0 until the required length is reached.

        Args:
            log_probs: The log_probs tensor of shape (max_length, n_trajectories) to extend.
            new_max_length: The new length of the log_probs tensor.

        Returns: The extended log_probs tensor of shape (new_max_length, n_trajectories).

        """

        max_length, n_trajectories = log_probs.shape
        if max_length >= new_max_length:
            return log_probs
        else:
            new_log_probs = torch.cat(
                (
                    log_probs,
                    torch.full(
                        size=(
                            new_max_length - log_probs.shape[0],
                            log_probs.shape[1],
                        ),
                        fill_value=0,
                        dtype=torch.float,
                        device=log_probs.device,
                    ),
                ),
                dim=0,
            )
            assert new_log_probs.shape == (new_max_length, n_trajectories)
            return new_log_probs

    def extend(self, other: Trajectories) -> None:
        """Extend the trajectories with another set of trajectories.

        Extends along all attributes in turn (actions, states, when_is_done, log_probs,
        log_rewards).

        Args:
            other: an external set of Trajectories.
        """
        if len(other) == 0:
            return

        # TODO: The replay buffer is storing `dones` - this wastes a lot of space.
        self.actions.extend(other.actions)
        self.states.extend(other.states)  # n_trajectories comes from this.
        self.when_is_done = torch.cat((self.when_is_done, other.when_is_done), dim=0)

        # For log_probs, we first need to make the first dimensions of self.log_probs
        # and other.log_probs equal (i.e. the number of steps in the trajectories), and
        # then concatenate them.
        new_max_length = max(self.log_probs.shape[0], other.log_probs.shape[0])
        self.log_probs = self.extend_log_probs(self.log_probs, new_max_length)
        other.log_probs = self.extend_log_probs(other.log_probs, new_max_length)
        self.log_probs = torch.cat((self.log_probs, other.log_probs), dim=1)

        # Concatenate log_rewards of the trajectories.
        if self._log_rewards is not None and other._log_rewards is not None:
            self._log_rewards = torch.cat(
                (self._log_rewards, other._log_rewards),
                dim=0,
            )
        # Will not be None if object is initialized as empty.
        else:
            self._log_rewards = None

        # Ensure log_probs/rewards are the correct dimensions. TODO: Remove?
        if self.log_probs.numel() > 0:
            assert self.log_probs.shape == self.actions.batch_shape

        if self.log_rewards is not None:
            assert len(self.log_rewards) == self.actions.batch_shape[-1]

        # Either set, or append, estimator outputs if they exist in the submitted
        # trajectory.
        if self.estimator_outputs is None and other.estimator_outputs is not None:
            self.estimator_outputs = other.estimator_outputs
        elif self.estimator_outputs is not None and other.estimator_outputs is not None:
            batch_shape = self.actions.batch_shape
            n_bs = len(batch_shape)

            # Cast other to match self.
            output_dtype = self.estimator_outputs.dtype
            other.estimator_outputs = other.estimator_outputs.to(dtype=output_dtype)

            if n_bs == 1:
                # Concatenate along the only batch dimension.
                self.estimator_outputs = torch.cat(
                    (self.estimator_outputs, other.estimator_outputs),
                    dim=0,
                )

            elif n_bs == 2:
                # Concatenate along the first dimension, padding where required.
                self_dim0 = self.estimator_outputs.shape[0]
                other_dim0 = other.estimator_outputs.shape[0]
                if self_dim0 != other_dim0:
                    # We need to pad the first dimension on either self or other.
                    required_first_dim = max(self_dim0, other_dim0)

                    if self_dim0 < other_dim0:
                        self.estimator_outputs = pad_dim0_to_target(
                            self.estimator_outputs,
                            required_first_dim,
                        )

                    elif self_dim0 > other_dim0:
                        other.estimator_outputs = pad_dim0_to_target(
                            other.estimator_outputs,
                            required_first_dim,
                        )

                # Concatenate the tensors along the second dimension.
                self.estimator_outputs = torch.cat(
                    (self.estimator_outputs, other.estimator_outputs),
                    dim=1,
                )

            # Sanity check. TODO: Remove?
            assert self.estimator_outputs.shape[:n_bs] == batch_shape

    def to_transitions(self) -> Transitions:
        """Returns a `Transitions` object from the trajectories."""
        if self.conditioning is not None:
            traj_len = self.actions.batch_shape[0]
            expand_dims = (traj_len,) + tuple(self.conditioning.shape)
            conditioning = self.conditioning.unsqueeze(0).expand(expand_dims)[
                ~self.actions.is_dummy
            ]
        else:
            conditioning = None

        states = self.states[:-1][~self.actions.is_dummy]
        next_states = self.states[1:][~self.actions.is_dummy]
        actions = self.actions[~self.actions.is_dummy]
        is_done = (
            next_states.is_sink_state
            if not self.is_backward
            else next_states.is_initial_state
        )
        if self._log_rewards is None:
            log_rewards = None
        else:
            log_rewards = torch.full(
                actions.batch_shape,
                fill_value=-float("inf"),
                dtype=torch.float,
                device=actions.device,
            )
            # TODO: Can we vectorize this?
            log_rewards[is_done] = torch.cat(
                [
                    self._log_rewards[self.when_is_done == i]
                    for i in range(self.when_is_done.max() + 1)
                ],
                dim=0,
            )

        # Initialize log_probs None if not available
        if has_log_probs(self):
            log_probs = self.log_probs[~self.actions.is_dummy]
        else:
            log_probs = None

        return Transitions(
            env=self.env,
            states=states,
            conditioning=conditioning,
            actions=actions,
            is_done=is_done,
            next_states=next_states,
            is_backward=self.is_backward,
            log_rewards=log_rewards,
            log_probs=log_probs,
        )

    def to_states(self) -> States:
        """Returns a `States` object from the trajectories, containing all states in the trajectories"""
        states = self.states.flatten()
        return states[~states.is_sink_state]

    def to_state_pairs(self) -> StatePairs[DiscreteStates]:
        """Converts a batch of trajectories into a batch of training samples.

        Returns:
            StatePairs: A StatePairs object containing intermediary and terminating states.
        """
        states = self.to_states()
        if not isinstance(states, DiscreteStates):
            raise TypeError("to_state_pairs only works with DiscreteStates")

        if self.conditioning is not None:
            traj_len = self.states.batch_shape[0]
            expand_dims = (traj_len,) + tuple(self.conditioning.shape)
            intermediary_conditioning = self.conditioning.unsqueeze(0).expand(
                expand_dims
            )[~states.is_sink_state & ~states.is_initial_state]
            conditioning = self.conditioning  # n_final_states == n_trajectories.
        else:
            intermediary_conditioning = None
            conditioning = None

        intermediary_states = states[~states.is_sink_state & ~states.is_initial_state]
        terminating_states = self.last_states

        # Ensure both states are DiscreteStates
        if not isinstance(intermediary_states, DiscreteStates) or not isinstance(
            terminating_states, DiscreteStates
        ):
            raise TypeError(
                "Both intermediary and terminating states must be DiscreteStates"
            )

        return StatePairs[DiscreteStates](
            env=self.env,
            intermediary_states=intermediary_states,
            terminating_states=terminating_states,
            intermediary_conditioning=intermediary_conditioning,
            terminating_conditioning=conditioning,
            log_rewards=self.log_rewards,
        )

    def reverse_backward_trajectories(self, debug: bool = False) -> Trajectories:
        """Return a reversed version of the backward trajectories."""
        assert self.is_backward, "Trajectories must be backward."

        # env.sf should never be None unless something went wrong during class
        # instantiation.
        if self.env.sf is None:
            raise AttributeError(
                "Something went wrong during the instantiation of environment {}".format(
                    self.env
                )
            )

        # Compute sequence lengths and maximum length
        seq_lengths = self.when_is_done  # shape (n_trajectories,)
        max_len = int(seq_lengths.max().item())

        # Get actions and states
        actions = self.actions.tensor  # shape (max_len, n_trajectories *action_dim)
        states = self.states.tensor  # shape (max_len + 1, n_trajectories, *state_dim)

        # Initialize new actions and states
        new_actions = self.env.dummy_action.repeat(max_len + 1, len(self), 1).to(actions)
        # shape (max_len + 1, n_trajectories, *action_dim)
        new_states = self.env.sf.repeat(max_len + 2, len(self), 1).to(states)
        # shape (max_len + 2, n_trajectories, *state_dim)

        # Create helper indices and masks
        idx = torch.arange(max_len).unsqueeze(1).expand(-1, len(self)).to(seq_lengths)
        rev_idx = seq_lengths - 1 - idx  # shape (max_len, n_trajectories)
        mask = rev_idx >= 0  # shape (max_len, n_trajectories)
        rev_idx[:, 1:] += seq_lengths.cumsum(0)[:-1]

        # Transpose for easier indexing
        actions = actions.transpose(0, 1)
        # shape (n_trajectories, max_len, *action_dim)
        new_actions = new_actions.transpose(0, 1)
        # shape (n_trajectories, max_len + 1, *action_dim)
        states = states.transpose(0, 1)
        # shape (n_trajectories, max_len + 1, *state_dim)
        new_states = new_states.transpose(0, 1)
        # shape (n_trajectories, max_len + 2, *state_dim)
        rev_idx = rev_idx.transpose(0, 1)
        mask = mask.transpose(0, 1)

        # Assign reversed actions to new_actions
        new_actions[:, :-1][mask] = actions[mask][rev_idx[mask]]
        new_actions[torch.arange(len(self)), seq_lengths] = self.env.exit_action

        # Assign reversed states to new_states
        assert isinstance(states[:, -1], torch.Tensor)
        assert isinstance(
            self.env.s0, torch.Tensor
        ), "reverse_backward_trajectories not supported for Graph trajectories"
        assert torch.all(states[:, -1] == self.env.s0), "Last state must be s0"
        new_states[:, 0] = self.env.s0
        new_states[:, 1:-1][mask] = states[:, :-1][mask][rev_idx[mask]]

        # Transpose back
        new_actions = new_actions.transpose(
            0, 1
        )  # shape (max_len + 1, n_trajectories, *action_dim)
        new_states = new_states.transpose(
            0, 1
        )  # shape (max_len + 2, n_trajectories, *state_dim)

        reversed_trajectories = Trajectories(
            env=self.env,
            states=self.env.states_from_tensor(new_states),
            conditioning=self.conditioning,
            actions=self.env.actions_from_tensor(new_actions),
            when_is_done=self.when_is_done + 1,
            is_backward=False,
            log_rewards=self.log_rewards,
            log_probs=None,  # We can't simply pass the trajectories.log_probs
            # Since `log_probs` is assumed to be the forward log probabilities.
            # FIXME: To resolve this, we can save log_pfs and log_pbs in the trajectories object.
            estimator_outputs=None,  # Same as `log_probs`.
        )

        # ------------------------------ DEBUG ------------------------------
        # If `debug` is True (expected only when testing), compare the
        # vectorized approach's results (above) to the for-loop results (below).
        if debug:
            _new_actions = self.env.dummy_action.repeat(max_len + 1, len(self), 1).to(
                actions
            )  # shape (max_len + 1, n_trajectories, *action_dim)
            _new_states = self.env.sf.repeat(max_len + 2, len(self), 1).to(
                states
            )  # shape (max_len + 2, n_trajectories, *state_dim)

            for i in range(len(self)):
                _new_actions[self.when_is_done[i], i] = self.env.exit_action
                _new_actions[: self.when_is_done[i], i] = self.actions.tensor[
                    : self.when_is_done[i], i
                ].flip(0)

                _new_states[: self.when_is_done[i] + 1, i] = self.states.tensor[
                    : self.when_is_done[i] + 1, i
                ].flip(0)

            assert torch.all(new_actions == _new_actions)
            assert torch.all(new_states == _new_states)

        return reversed_trajectories


def pad_dim0_to_target(a: torch.Tensor, target_dim0: int) -> torch.Tensor:
    """Pads tensor a to match the dimension of b."""
    assert a.shape[0] < target_dim0, "a is already larger than target_dim0!"
    pad_dim = target_dim0 - a.shape[0]
    pad_dim_full = (pad_dim,) + tuple(a.shape[1:])
    output_padding = torch.full(
        pad_dim_full,
        fill_value=-float("inf"),
        dtype=a.dtype,  # TODO: This isn't working! Hence the cast below...
        device=a.device,
    )
    return torch.cat((a, output_padding), dim=0)
