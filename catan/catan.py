import dataclasses
import logging
import random
import sys
from collections import defaultdict
from contextlib import contextmanager
from functools import partial, singledispatch
from importlib import import_module
from itertools import chain, count
from pathlib import Path
from typing import Dict, List, Optional, Union, Sequence, Iterable, Tuple

from ops import CharmBase
from scenario import *
from scenario.state import _CharmSpec, _DCBase

logger = logging.getLogger("catan")


@dataclasses.dataclass(frozen=True)
class App(_DCBase):
    charm: _CharmSpec
    leader_id: int = None
    alias: str = None

    @staticmethod
    def from_path(path: Union[str, Path], name: str = None, patches=None):
        charm_root = Path(path)
        if not charm_root.exists():
            raise RuntimeError(
                f"{charm_root} not found: cannot import charm from path."
            )
        sources_roots = tuple(
            map(
                str,
                [
                    charm_root / "src",
                    charm_root / "lib",
                ],
            )
        )
        sys.path.extend(sources_roots)

        if patches:
            for patch in patches:
                patch.__enter__()

        module = import_module("charm")
        for sroot in sources_roots:
            sys.path.remove(sroot)

        charm_type = [
            t
            for t in module.__dict__.values()
            if isinstance(t, type)
            and issubclass(t, CharmBase)
            and not t.__name__ == "CharmBase"
        ]

        if not charm_type:
            raise RuntimeError(f"No charm could be loaded from {module}.")
        if len(charm_type) > 1:
            raise RuntimeError(f"Too many charms found in {module}: {charm_type}")

        spec = _CharmSpec.autoload(charm_type[0])

        # unimport charm else the next 'import' will pick it up again
        sys.modules.pop("charm")
        return App(spec, alias=name or spec.meta["name"])

    def binding(self, name: str):
        raise NotImplementedError()

    @property
    def name(self):
        return self.alias or self.charm.meta["name"]

    def __eq__(self, other: "App"):
        return self.name == other.name

    def __hash__(self):
        # app names are going to be model-unique anyway.
        return hash(self.name)

    def __repr__(self):
        return f"<App: {self.name}>"


@dataclasses.dataclass(frozen=True)
class Binding(_DCBase):
    app: App
    relation: Relation


@dataclasses.dataclass(frozen=True)
class Integration(_DCBase):
    """Juju integration."""

    binding1: Binding
    binding2: Binding

    @property
    def apps(self) -> Tuple[App, App]:
        """Apps partaking in this integration."""
        return self.binding1.app, self.binding2.app


@dataclasses.dataclass(frozen=True)
class _HistoryItem(_DCBase):
    event: Event
    app: App
    unit_id: int
    state_out: State


@dataclasses.dataclass(frozen=True)
class ModelState(_DCBase):
    unit_states: Dict[App, Dict[int, State]] = dataclasses.field(default_factory=dict)
    """Mapping from apps to the states of their units."""

    integrations: List[Integration] = dataclasses.field(default_factory=list)
    model: Model = dataclasses.field(default_factory=Model)

    def __str__(self):
        no_units = sum(map(len, self.unit_states.values()))
        return f"<ModelState ({len(self.unit_states)} apps, {no_units} units)>"


_qitem_counter = count()


@dataclasses.dataclass
class _QueueItem:
    event: Event
    app: Optional[App]
    unit_id: Optional[Optional[int]]
    group: Optional[bool]
    _index: int = dataclasses.field(default_factory=lambda: next(_qitem_counter))

    def __repr__(self):
        return f"Q<{self.event.name}({self.group or '-'}/{self._index})>"


class Catan:
    """Model-level State convergence tool.

    Settlers of Juju unite!
    Like scenario, but for multiple charms.
    """

    def __init__(self, model_state: Optional[ModelState] = None):
        self._model_state = model_state or ModelState()

        # used to keep track of states we remove from self._model_state (e.g. with remove_unit)
        # but we still need in order to be able to scenario the charm.
        self._dead_unit_states: Dict[App, Dict[int, State]] = defaultdict(dict)
        self._event_queue: List[_QueueItem] = []
        self._emitted: List[_HistoryItem] = []

        self._fixed_sequence_counter = 0
        self._current_group = None

    @property
    def model_state(self) -> ModelState:
        """The model state attached to this Catan."""
        return self._model_state

    def queue(self, event: Union[Event, str], app: App = None, unit_id: int = None):
        """Queue an event for emission on an app or a unit."""
        self._queue(self._model_state, event, app, unit_id)

    def _queue(
        self,
        model_state: ModelState,
        event: Union[str, Event],
        app: Optional[App] = None,
        unit_id: Optional[int] = None,
    ):

        if isinstance(event, str):
            event = Event(event)

        qitem = _QueueItem(event, app, unit_id, group=self._current_group)
        expanded = self._expand_queue_item(model_state, qitem)
        self._event_queue.extend(expanded)
        return expanded

    def _expand_queue_item(
        self, model_state: ModelState, item: _QueueItem
    ) -> Iterable[_QueueItem]:
        """Expand the queue item until all elements are explicit."""

        def _expand(ms: ModelState, qitem: _QueueItem):
            event = qitem.event
            app = qitem.app
            unit_id = qitem.unit_id
            group = qitem.group

            if app is not None:
                if unit_id is not None:
                    return [qitem]
                units = (
                    ms.unit_states[app]
                    if app in ms.unit_states
                    else self._dead_unit_states[app]
                )
                return [_QueueItem(event, app, unit_, group) for unit_ in units]
            return chain(
                *(
                    _expand(ms, _QueueItem(event, app, None, group))
                    for app in ms.unit_states
                )
            )

        return chain(*map(partial(_expand, model_state), [item]))

        # def _sorting(elem: _QueueItem):
        #     # sort by group event priority, then app name, then unit ID.
        #     # app name and unit ID could be ignored, but we like things deterministic
        #     return (-self._get_priority(elem.event), elem.app.name, elem.unit_id)
        #
        # if self._queue_sorting:
        #     final = sorted(expanded, key=_sorting)
        # else:
        #     final = list(expanded)

    @contextmanager
    def fixed_sequence(self):
        """Context to keep together all events queued together."""
        self._current_group = self._fixed_sequence_counter
        logger.debug(f"starting group sequence {self._fixed_sequence_counter}")

        yield

        self._fixed_sequence_counter += 1
        self._current_group = None
        logger.debug(f"exiting group sequence {self._fixed_sequence_counter}")

    @staticmethod
    def _get_priority(event: Event):
        suffixes_to_priority = {
            "_action": 150,
            "relation_created": 100,
            "relation_broken": 99,
            "relation_joined": 98,
            "relation_departed": 98,
            "relation_changed": 50,
            "config_changed": 49,
            "update_status": 0,
        }
        for suffix, prio in suffixes_to_priority.items():
            if event.name.endswith(suffix):
                return prio
        return 50

    def _get_next_queue_item(self):
        if self._event_queue:
            return self._event_queue.pop(0)

    @property
    def emitted(self) -> List[_HistoryItem]:
        """Inspect all events that have been emitted so far by this Catan."""
        return self._emitted

    def settle(self) -> ModelState:
        """Settle Catan."""
        model_state = self._initial_sync()

        i = 0

        # todo: infinite loop detection.
        #  e.g. relation-changed ping-pong

        while item := self._get_next_queue_item():
            i += 1
            logger.info(f"processing item {item} ({i}/{len(self._event_queue)})")

            ms_out = self._fire(model_state, item.event, item.app, item.unit_id)
            ms_out_synced = self._model_reconcile(ms_out, item.app, item.unit_id)
            model_state = ms_out_synced

        if i == 0:
            logger.warning(
                "Event queue empty: converged in zero iterations. "
                "Model state unchanged (modulo initial sync)."
            )
            self._model_state = model_state
            return model_state

        logger.info(f"Catan settled in {i} iterations")
        log = "\t" + "\n\t".join(self._emitted_repr)
        logger.info(f"Emission log: \n {log}")
        self._model_state = model_state
        return model_state

    @staticmethod
    def _run_scenario(app: App, unit_id: int, unit_state: State, event: Event) -> State:
        logger.info("running scenario...")
        context = Context(
            app.charm.charm_type,
            # pass meta, config, actions instead of letting _CharmSpec.autoload() again because
            # we've already removed the path from sys.modules, so attempting to find the source
            # of charm_type would incorrectly classify it as a built-in type.
            meta=app.charm.meta,
            config=app.charm.config,
            actions=app.charm.actions,
            app_name=app.name,
            unit_id=unit_id,
        )
        if event._is_action_event:  # noqa
            return context.run_action(event.action, unit_state).state
        else:
            return context.run(event, unit_state)

    def _fire(
        self, model_state: ModelState, event: Event, app: App, unit_id: int
    ) -> ModelState:
        logger.info(f"firing {event} on {app}:{unit_id}")
        # don't mutate: replace.
        ms_out = model_state.copy()
        dead_unit = False

        try:
            units = ms_out.unit_states[app]
            state_in = units[unit_id]
        except KeyError as e:
            try:
                state_in = self._dead_unit_states[app][unit_id]
                dead_unit = True
            except KeyError:
                raise RuntimeError(
                    f"{app}/{unit_id} not found in current model_state or dead_units_states."
                ) from e

        state_out = self._run_scenario(app, unit_id, state_in, event)

        self._emitted.append(_HistoryItem(event, app, unit_id, state_out))

        if not dead_unit:
            units[unit_id] = state_out  # noqa
            ms_out.unit_states[app] = units

        return ms_out

    def _initial_sync(self) -> ModelState:
        """Bring the unit states in sync with what's in the Integrations."""

        def _sync(relation_1: Relation, relation_2: Relation, states: Dict[int, State]):
            # relation = relation_1.replace(remote_app_data=relation_2.local_app_data)
            return {
                uid: s.replace(relations=s.relations + [relation_1])
                for uid, s in states.items()
            }

        model_state = self._model_state
        new_states = model_state.unit_states.copy()

        for i in model_state.integrations:
            b1 = i.binding1
            b2 = i.binding2
            new_states[b1.app] = _sync(b1.relation, b2.relation, new_states[b1.app])
            new_states[b2.app] = _sync(b2.relation, b1.relation, new_states[b2.app])

        return model_state.replace(unit_states=new_states)

    def _model_reconcile(self, model_state_out: ModelState, app: App, unit_id: int):
        """Check what has changed between the model states and queue any generated events."""
        # fixme: no model_state_out mutation
        for i in model_state_out.integrations:
            self._reconcile_integration(model_state_out, i, app, unit_id)
        return model_state_out

    def _reconcile_integration(
        self,
        model_state_out: "ModelState",
        integration: Integration,
        app: "App" = None,
        unit_id: int = None,
        queue: bool = True,
    ):
        """Sync this integration's bindings and queue any events on Catan."""

        def _sync(states_from, states_to, binding_from: Binding, binding_to: Binding):
            # simplifying a bit:
            # if b1's local app data is different from b2's remote app data:
            # copy it over and notify all b2 apps of the change in remote app data

            # this is the unit that's made changes to the state.
            # If not given, it means we're doing an initial sync (not as a consequence of a
            # specific charm executing) and so any unit will do.
            # this ASSUMES that all input states will be equivalent.
            master_state = states_from[unit_id or 0]
            any_b1_relation = master_state.get_relations(
                binding_from.relation.endpoint
            )[0]

            # not nice, but inevitable?
            any_b2_state = next(iter(states_to.values()))
            any_b2_relation = any_b2_state.get_relations(binding_to.relation.endpoint)[
                0
            ]

            if any_b1_relation.local_app_data == any_b2_relation.remote_app_data:
                # no changes!
                logger.debug("nothing to sync: relation data didn't change")
                return

            # copy over all relations; the one this endpoint is about would be enough,
            # but we don't quite care as the rest shouldn't have changed either.
            new_states_1 = {
                uid: follower_state.replace(relations=master_state.relations)
                for uid, follower_state in states_from.items()
            }
            # we copy the local app data of any relation in the master state
            local_app_data = any_b1_relation.local_app_data
            # todo sync unit data

            # now we copy the local app data of the master state into the remote app data of each state
            # of the remote app
            # any state will do, they should all be
            # the same where the relation is concerned
            # all relations in binding_to stay the same for other endpoints, but for
            # the binding_to endpoint that's being changed,
            # we replace remote app data with binding_from's local app data.
            any_state_to = next(iter(states_to.values()))
            new_relations_b2 = [
                r
                for r in any_state_to.relations
                if r.endpoint != binding_to.relation.endpoint
            ] + [
                r.replace(remote_app_data=local_app_data)
                for r in any_state_to.relations
                if r.endpoint == binding_to.relation.endpoint
            ]
            new_states_2 = {
                uid: unit_state.replace(relations=new_relations_b2)
                for uid, unit_state in states_to.items()
            }

            model_state_out.unit_states[binding_from.app] = new_states_1
            model_state_out.unit_states[binding_to.app] = new_states_2

            if queue:
                self.queue(any_b2_relation.changed_event, binding_to.app)

        # we assume that every state transition only touches a single state: namely that of
        # app/unit.

        b1, b2 = integration.binding1, integration.binding2
        states_out_b1 = model_state_out.unit_states[b1.app]
        states_out_b2 = model_state_out.unit_states[b2.app]

        if app is None or app == b1.app:
            _sync(states_out_b1, states_out_b2, b1, b2)

        if app is None or app == b2.app:
            _sync(states_out_b2, states_out_b1, b2, b1)

    def _final_sync(self):
        """Bring the Integrations in sync with what's in model's unit states."""
        model_state = self._model_state
        new_integrations = []

        for i in model_state.integrations:
            b1 = i.binding1
            b2 = i.binding2
            new_integrations.append(i.replace())

        return model_state.replace(integrations=new_integrations)

    def _add_integration(self, integration: Integration):
        """Add an integration to the model."""
        ms_out = self._model_state.replace(
            integrations=self._model_state.integrations + [integration]
        )
        for relation, app in (
            (integration.binding1.relation, integration.binding1.app),
            (integration.binding2.relation, integration.binding2.app),
        ):
            self._queue(ms_out, relation.created_event, app)
            self._queue(ms_out, relation.joined_event, app)

            self._queue(ms_out, relation.changed_event, app)
        self._model_state = ms_out
        return ms_out

    def integrate(
        self,
        app1: App,
        endpoint1: str,
        app2: App,
        endpoint2: str,
    ):
        """Integrate two apps."""
        relation1 = Relation(endpoint1)
        relation2 = Relation(endpoint2)
        integration = Integration(Binding(app1, relation1), Binding(app2, relation2))
        return self._add_integration(integration)

    def disintegrate(self, integration: Integration) -> ModelState:
        """Remove an integration."""
        model_state = self._model_state
        app1, app2 = integration.apps

        for app, relation in (
            (app1, integration.binding1.relation),
            (app2, integration.binding2.relation),
        ):
            # todo do this for all <other app> units
            self._queue(model_state, relation.departed_event, app)
            self._queue(model_state, relation.broken_event, app)

        ms_out = model_state.replace(
            integrations=[i for i in model_state.integrations if i is not integration]
        )
        self._model_state = ms_out
        return ms_out

    def get_integration(self, app1: App, endpoint: str, app2: App) -> Integration:
        """Get an integration."""
        to_remove = None
        ms = self._model_state

        for integration in ms.integrations:
            if (
                integration.binding1.app == app1
                and integration.binding2.app == app2
                and integration.binding1.relation.endpoint == endpoint
            ):
                to_remove = integration
                break
            if (
                integration.binding1.app == app2
                and integration.binding2.app == app1
                and integration.binding1.relation.endpoint == endpoint
            ):
                app1, app2 = app2, app1
                to_remove = integration
                break

        if not to_remove:
            raise RuntimeError(f"Integration not found: {app1}:{endpoint} --> {app2}")

        return to_remove

    def run_action(
        self,
        action: Union[str, Action],
        app: App,
        unit: Optional[int] = None,
    ):
        """Run an action on all units or a specific one."""
        if not isinstance(action, Action):
            action = Action(action)

        self._queue(self._model_state, action.event, app, unit)

    def queue_setup_sequence(self, app: App, unit: Optional[int] = None):
        """Queues setup phase event sequence for this app/unit."""

        model_state = self._model_state

        if unit is None:
            for unit_id in model_state.unit_states[app]:
                self.queue_setup_sequence(app, unit_id)
            return

        with self.fixed_sequence():
            self._queue(model_state, "install", app, unit)
            # todo storage attached events
            # todo relation events
            is_leader = model_state.unit_states[app][unit].leader
            self._queue(
                model_state,
                "leader-elected" if is_leader else "leader-settings-changed",
                app,
                unit,
            )
            self._queue(model_state, "config-changed", app, unit)
            self._queue(model_state, "start", app, unit)

    def queue_teardown_sequence(self, app: App, unit: Optional[int] = None):
        """Queues teardown phase event sequence for this app/unit."""
        model_state = self._model_state

        if unit is None:
            for unit_id in model_state.unit_states[app]:
                self.queue_teardown_sequence(app, unit_id)
            return

        with self.fixed_sequence():
            # todo storage detached events
            # todo relation broken events
            self._queue(model_state, "stop", app, unit)
            self._queue(model_state, "remove", app, unit)

    def add_unit(
        self,
        app: App,
        unit: int,
        state: State,
    ):
        """Adds a unit to this application."""
        model_state = self._model_state
        new_states = model_state.unit_states.copy()

        if unit in new_states[app]:
            raise RuntimeError(f"{app.name}/{unit} exists already in the model state")

        if state.leader:
            raise RuntimeError("new units cannot join as leaders.")

        new_states[app][unit] = state
        new_model_state = model_state.replace(unit_states=new_states)

        self._model_state = new_model_state
        self.queue_setup_sequence(app, unit)
        return new_model_state

    def imatrix_clear(self, *app: App) -> List[Integration]:
        """Remove all relations (involving app)."""
        gone = []
        for integration in list(self._model_state.integrations):
            if app:
                if any(a in integration.apps for a in app) and integration not in gone:
                    self.disintegrate(integration)
                    gone.append(integration)
            else:
                self.disintegrate(integration)
                gone.append(integration)
        return gone

    def imatrix_fill(self, *app: App) -> List[Integration]:
        """Cross-relate all that's relatable (only including *app)."""

        # mapping from apps to interfaces to endpoints
        providers = {}
        requirers = {}

        pool = app or list(self._model_state.unit_states)

        for _app in pool:
            # mapping from interfaces to app, endpoint pairs
            providers_from_app = defaultdict(list)
            for endpoint, ep_meta in _app.charm.meta.get("provides", {}).items():
                providers_from_app[ep_meta["interface"]].append((_app, endpoint))
            providers.update(providers_from_app)

            requirers_from_app = defaultdict(list)
            for endpoint, ep_meta in _app.charm.meta.get("requires", {}).items():
                requirers_from_app[ep_meta["interface"]].append((_app, endpoint))
            requirers.update(requirers_from_app)

        # interfaces that have both a requirer and a provider in the pool of apps under consideration
        # sorting is to make our lives easier in testing
        shared_interfaces = sorted(set(providers).intersection(set(requirers)))

        integrations = []
        for interface in shared_interfaces:
            for prov_app, prov_ep in providers[interface]:
                for req_app, req_ep in requirers[interface]:
                    integration = Integration(
                        Binding(prov_app, Relation(prov_ep)),
                        Binding(req_app, Relation(req_ep)),
                    )
                    self._add_integration(integration)
                    integrations.append(integration)

        return integrations

    def remove_app(self, app: App):
        """Remove an app."""
        model_state = self._model_state
        new_states = model_state.unit_states.copy()

        if app not in new_states:
            raise RuntimeError(f"app {app} not in ModelState")

        all_unit_states = new_states.pop(app)
        # in case we've previously killed some units of this app:
        all_unit_states.update(self._dead_unit_states[app])
        self._dead_unit_states[app] = all_unit_states

        new_model_state: ModelState = model_state.replace(unit_states=new_states)
        # set this first so that queue_setup_sequence will use it
        self._model_state = new_model_state

        # remove all integrations where app is involved
        self.imatrix_clear(app)

        # we could call queue_teardown_sequence(app), but depending on who's the leader that might
        # trigger a lot more events than we'd like. If we're destroying the app there's no point
        # in electing a new leader each time, so instead we nuke the leader last,
        # to prevent any bouncing.
        follower_ids = list(all_unit_states)
        leader_id = [i for i, s in all_unit_states.items() if s.leader][0]
        follower_ids.remove(leader_id)
        for follower_id in follower_ids:
            self.queue_teardown_sequence(app, follower_id)
        self.queue_teardown_sequence(app, leader_id)

        return new_model_state

    def remove_unit(self, app: App, unit: int):
        """Remove an app."""
        model_state = self._model_state
        new_states = model_state.unit_states.copy()

        if app not in new_states:
            raise RuntimeError(f"app {app} not in ModelState")

        unit_state = new_states[app].pop(unit)
        self._dead_unit_states[app][unit] = unit_state

        new_model_state = model_state.replace(unit_states=new_states)

        if unit_state.leader:
            # killing the leader!
            if new_states[app]:
                new_leader_id = random.choice(list(new_states[app]))
                logger.debug(
                    f"killing the leader. Electing {new_leader_id} instead. "
                    f"Long live {app}/{new_leader_id}!"
                )

                new_states[app][new_leader_id] = new_states[app][new_leader_id].replace(
                    leader=True
                )
                self._queue(new_model_state, "leader-elected", app, new_leader_id)

            else:
                logger.debug(f"killing the last unit and leader, {app} scaled to zero.")

            self._queue(new_model_state, "leader-settings-changed", app, unit)

        # set this first so that queue_setup_sequence will use it
        self._model_state = new_model_state
        self.queue_teardown_sequence(app, unit)

        return new_model_state

    def deploy(
        self,
        app: App,
        ids: Sequence[int],
        state_template: State,
        leader_id: Optional[int] = None,
    ):
        """Deploy an app."""
        model_state = self._model_state
        new_states = model_state.unit_states.copy()

        if app in new_states:
            raise RuntimeError(f"app {app} already in input ModelState")

        if not leader_id:
            leader_id = ids[0]
            logger.debug(
                f"no leader_id provided: leader will be {app.name}/{leader_id}"
            )

        new_states[app] = {
            id_: state_template.replace(leader=(leader_id == id_)) for id_ in ids
        }
        new_model_state = model_state.replace(unit_states=new_states)

        # set this first so that queue_setup_sequence will use it
        self._model_state = new_model_state
        self.queue_setup_sequence(app)

        return new_model_state

    @property
    def _emitted_repr(self):
        return [f"{e.app.name}/{e.unit_id} :: {e.event.path}" for e in self._emitted]

    def shuffle(self, respect_sequences: bool = True):
        """In-place-shuffle self._event_queue."""
        if respect_sequences:
            groups = defaultdict(list)

            for item in self._event_queue:
                if item.group:
                    groups[item.group].append(item)
                else:
                    # ungrouped elements go to -1
                    groups[-1].append(item)

            queue = []

            while groups:
                # pop the first element from a randomly selected ordered sequence
                target = random.choice(list(groups))
                sequence = groups[target]
                queue.append(sequence.pop(0))

                if not sequence:
                    del groups[target]

            self._event_queue = queue

        else:
            random.shuffle(self._event_queue)
