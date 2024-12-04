from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime
import json
import threading

import grpc

from optuna.distributions import distribution_to_json
from optuna.distributions import json_to_distribution
from optuna.storages import RDBStorage
from optuna.storages.grpc import _api_pb2
from optuna.storages.grpc._api_pb2_grpc import add_StorageServiceServicer_to_server
from optuna.storages.grpc._api_pb2_grpc import StorageServiceServicer
from optuna.study._study_direction import StudyDirection
from optuna.trial._frozen import FrozenTrial
from optuna.trial._state import TrialState


DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


class _StudyInfo:
    def __init__(self) -> None:
        # Trial number to corresponding FrozenTrial.
        self.trials: dict[int, FrozenTrial] = {}
        # A list of trials and the last trial number which require storage access to read latest
        # attributes.
        self.unfinished_trial_ids: set[int] = set()
        self.last_finished_trial_id: int = -1
        self.directions: list[StudyDirection] | None = None
        self.name: str | None = None


class OptunaStorageProxyService(StorageServiceServicer):
    def __init__(self, storage_url: str) -> None:
        self._backend = RDBStorage(storage_url)
        self._studies: dict[int, _StudyInfo] = {}
        self._trial_id_to_study_id_and_number: dict[int, tuple[int, int]] = {}
        self._study_id_and_number_to_trial_id: dict[tuple[int, int], int] = {}
        self._lock = threading.Lock()

    def CreateNewStudy(
        self,
        request: _api_pb2.CreateNewStudyRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.CreateNewStudyReply:
        study_id = self._backend.create_new_study(
            directions=request.directions,
            study_name=request.study_name,
        )

        with self._lock:
            study = _StudyInfo()
            study.directions = [
                StudyDirection.MINIMIZE if d == _api_pb2.MINIMIZE else StudyDirection.MAXIMIZE
                for d in request.directions
            ]
            study.name = request.study_name
            self._studies[study_id] = study

        return _api_pb2.CreateNewStudyReply(study_id=study_id)

    def DeleteStudy(
        self,
        request: _api_pb2.DeleteStudyRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.DeleteStudyReply:
        study_id = request.study_id
        self._backend.delete_study(study_id)

        with self._lock:
            if request.study_id in self._studies:
                for trial_number in self._studies[study_id].trials:
                    trial_id = self._study_id_and_number_to_trial_id.get((study_id, trial_number))
                    if trial_id in self._trial_id_to_study_id_and_number:
                        del self._trial_id_to_study_id_and_number[trial_id]
                    if (study_id, trial_number) in self._study_id_and_number_to_trial_id:
                        del self._study_id_and_number_to_trial_id[(study_id, trial_number)]
                del self._studies[request.study_id]

        return _api_pb2.DeleteStudyReply()

    def SetStudyUserAttribute(
        self,
        request: _api_pb2.SetStudyUserAttributeRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.SetStudyUserAttributeReply:
        self._backend.set_study_user_attr(request.study_id, request.key, json.dumps(request.value))
        return _api_pb2.SetStudyUserAttributeReply()

    def SetStudySystemAttribute(
        self,
        request: _api_pb2.SetStudySystemAttributeRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.SetStudySystemAttributeReply:
        self._backend.set_study_system_attr(
            request.study_id, request.key, json.dumps(request.value)
        )
        return _api_pb2.SetStudySystemAttributeReply()

    def GetStudyIdFromName(
        self,
        request: _api_pb2.GetStudyIdFromNameRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.GetStudyIdFromNameReply:
        study_id = self._backend.get_study_id_from_name(request.study_name)
        return _api_pb2.GetStudyIdFromNameReply(study_id=study_id)

    def GetStudyNameFromId(
        self,
        request: _api_pb2.GetStudyNameFromIdRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.GetStudyNameFromIdReply:
        study_id = request.study_id

        with self._lock:
            if study_id in self._studies:
                name = self._studies[study_id].name
                if name is not None:
                    return _api_pb2.GetStudyNameFromIdReply(study_name=name)

        name = self._backend.get_study_name_from_id(study_id)
        with self._lock:
            if study_id not in self._studies:
                self._studies[study_id] = _StudyInfo()
            self._studies[study_id].name = name

        return _api_pb2.GetStudyNameFromIdReply(study_name=name)

    def GetStudyDirections(
        self,
        request: _api_pb2.GetStudyDirectionsRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.GetStudyDirectionsReply:
        study_id = request.study_id
        with self._lock:
            if study_id in self._studies:
                directions = self._studies[study_id].directions
                if directions is not None:
                    return _api_pb2.GetStudyDirectionsReply(
                        directions=[
                            (
                                _api_pb2.MINIMIZE
                                if d == StudyDirection.MINIMIZE
                                else _api_pb2.MAXIMIZE
                            )
                            for d in directions
                        ]
                    )

        directions = self._backend.get_study_directions(study_id)
        with self._lock:
            if study_id not in self._studies:
                self._studies[study_id] = _StudyInfo()
            self._studies[study_id].directions = directions

        return _api_pb2.GetStudyDirectionsReply(
            directions=[
                _api_pb2.MINIMIZE if d == StudyDirection.MINIMIZE else _api_pb2.MAXIMIZE
                for d in directions
            ]
        )

    def GetStudyUserAttributes(
        self,
        request: _api_pb2.GetStudyUserAttributesRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.GetStudyUserAttributesReply:
        attributes = self._backend.get_study_user_attrs(request.study_id)
        return _api_pb2.GetStudyUserAttributesReply(
            user_attributes={key: json.dumps(value) for key, value in attributes.items()}
        )

    def GetStudySystemAttributes(
        self,
        request: _api_pb2.GetStudySystemAttributesRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.GetStudySystemAttributesReply:
        attributes = self._backend.get_study_system_attrs(request.study_id)
        return _api_pb2.GetStudySystemAttributesReply(
            system_attributes={key: json.dumps(value) for key, value in attributes.items()}
        )

    def GetAllStudies(
        self,
        request: _api_pb2.GetAllStudiesRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.GetAllStudiesReply:
        studies = self._backend.get_all_studies()
        return _api_pb2.GetAllStudiesReply(
            frozen_studies=[
                _api_pb2.FrozenStudy(
                    study_id=study._study_id,
                    study_name=study.study_name,
                    directions=[
                        _api_pb2.MINIMIZE if d == StudyDirection.MINIMIZE else _api_pb2.MAXIMIZE
                        for d in study.directions
                    ],
                    user_attributes={
                        key: json.dumps(value) for key, value in study.user_attrs.items()
                    },
                    system_attributes={
                        key: json.dumps(value) for key, value in study.system_attrs.items()
                    },
                )
                for study in studies
            ]
        )

    def CreateNewTrial(
        self,
        request: _api_pb2.CreateNewTrialRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.CreateNewTrialReply:
        study_id = request.study_id

        template_trial = None
        if request.template_trial is not None:
            template_trial = _from_proto_frozen_trial(request.template_trial)

        frozen_trial = self._backend._create_new_trial(study_id, template_trial)
        trial_id = frozen_trial._trial_id
        with self._lock:
            if study_id not in self._studies:
                self._studies[study_id] = _StudyInfo()
            study = self._studies[study_id]
            self._add_trials_to_cache(study_id, [frozen_trial])
            # Since finished trials will not be modified by any worker, we do not
            # need storage access for them.
            if frozen_trial.state.is_finished():
                study.last_finished_trial_id = max(study.last_finished_trial_id, trial_id)
            else:
                study.unfinished_trial_ids.add(trial_id)

        return _api_pb2.CreateNewTrialReply(trial_id=trial_id)

    def SetTrialParameter(
        self,
        request: _api_pb2.SetTrialParameterRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.SetTrialParameterReply:
        trial_id = request.trial_id
        param_name = request.param_name
        param_value_internal = request.param_value_internal
        distribution = json_to_distribution(request.distribution)
        self._backend.set_trial_param(trial_id, param_name, param_value_internal, distribution)
        return _api_pb2.SetTrialParameterReply()

    def GetTrialIdFromStudyIdTrialNumber(
        self,
        request: _api_pb2.GetTrialIdFromStudyIdTrialNumberRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.GetTrialIdFromStudyIdTrialNumberReply:
        study_id = request.study_id
        trial_number = request.trial_number
        key = (study_id, trial_number)
        with self._lock:
            if key in self._study_id_and_number_to_trial_id:
                trial_id = self._study_id_and_number_to_trial_id[key]

        trial_id = self._backend.get_trial_id_from_study_id_trial_number(study_id, trial_number)
        return _api_pb2.GetTrialIdFromStudyIdTrialNumberReply(trial_id=trial_id)

    def SetTrialStateValues(
        self,
        request: _api_pb2.SetTrialStateValuesRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.SetTrialStateValuesReply:
        trial_id = request.trial_id
        state = request.state
        values = request.values
        trial_updated = self._backend.set_trial_state_values(
            trial_id, _from_proto_trial_state(state), values
        )
        return _api_pb2.SetTrialStateValuesReply(trial_updated=trial_updated)

    def SetTrialIntermediateValue(
        self,
        request: _api_pb2.SetTrialIntermediateValueRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.SetTrialIntermediateValueReply:
        trial_id = request.trial_id
        step = request.step
        intermediate_value = request.intermediate_value
        self._backend.set_trial_intermediate_value(trial_id, step, intermediate_value)
        return _api_pb2.SetTrialIntermediateValueReply()

    def SetTrialUserAttribute(
        self,
        request: _api_pb2.SetTrialUserAttributeRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.SetTrialUserAttributeReply:
        trial_id = request.trial_id
        key = request.key
        value = json.loads(request.value)
        self._backend.set_trial_user_attr(trial_id, key, value)
        return _api_pb2.SetTrialUserAttributeReply()

    def SetTrialSystemAttribute(
        self,
        request: _api_pb2.SetTrialSystemAttributeRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.SetTrialSystemAttributeReply:
        trial_id = request.trial_id
        key = request.key
        value = json.loads(request.value)
        self._backend.set_trial_system_attr(trial_id, key, value)
        return _api_pb2.SetTrialSystemAttributeReply()

    def GetTrial(
        self,
        request: _api_pb2.GetTrialRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.GetTrialReply:
        trial_id = request.trial_id
        with self._lock:
            trial = self._get_cached_trial(trial_id)

        if trial is None:
            trial = self._backend.get_trial(trial_id)

        assert trial is not None

        params = {}
        for key, value in trial.params.items():
            params[key] = trial.distributions[key].to_internal_repr(value)

        return _api_pb2.GetTrialReply(frozen_trial=_to_proto_frozen_trial(trial))

    def GetAllTrials(
        self,
        request: _api_pb2.GetAllTrialsRequest,
        context: grpc.ServicerContext,
    ) -> _api_pb2.GetAllTrialsReply:
        study_id = request.study_id
        deepcopy = request.deepcopy
        states = (
            [_from_proto_trial_state(state) for state in request.states]
            if request.states
            else None
        )
        self._read_trials_from_remote_storage(study_id)

        with self._lock:
            study = self._studies[study_id]

            if states is not None:
                trials_dict = {
                    number: t for number, t in study.trials.items() if t.state in states
                }
            else:
                trials_dict = study.trials
            trials = list(sorted(trials_dict.values(), key=lambda t: t.number))
            trials = copy.deepcopy(trials) if deepcopy else trials

        return _api_pb2.GetAllTrialsReply(
            frozen_trials=[_to_proto_frozen_trial(trial) for trial in trials]
        )

    def _get_cached_trial(self, trial_id: int) -> FrozenTrial | None:
        if trial_id not in self._trial_id_to_study_id_and_number:
            return None
        study_id, number = self._trial_id_to_study_id_and_number[trial_id]
        study = self._studies[study_id]
        return study.trials[number] if trial_id not in study.unfinished_trial_ids else None

    def _read_trials_from_remote_storage(self, study_id: int) -> None:
        with self._lock:
            if study_id not in self._studies:
                self._studies[study_id] = _StudyInfo()
            study = self._studies[study_id]
            trials = self._backend._get_trials(
                study_id,
                states=None,
                included_trial_ids=study.unfinished_trial_ids,
                trial_id_greater_than=study.last_finished_trial_id,
            )
            if not trials:
                return

            self._add_trials_to_cache(study_id, trials)
            for trial in trials:
                if not trial.state.is_finished():
                    study.unfinished_trial_ids.add(trial._trial_id)
                    continue

                study.last_finished_trial_id = max(study.last_finished_trial_id, trial._trial_id)
                if trial._trial_id in study.unfinished_trial_ids:
                    study.unfinished_trial_ids.remove(trial._trial_id)

    def _add_trials_to_cache(self, study_id: int, trials: list[FrozenTrial]) -> None:
        study = self._studies[study_id]
        for trial in trials:
            self._trial_id_to_study_id_and_number[trial._trial_id] = (
                study_id,
                trial.number,
            )
            self._study_id_and_number_to_trial_id[(study_id, trial.number)] = trial._trial_id
            study.trials[trial.number] = trial


def _to_proto_trial_state(state: TrialState) -> _api_pb2.TrialState:
    if state == TrialState.RUNNING:
        return _api_pb2.RUNNING
    if state == TrialState.COMPLETE:
        return _api_pb2.COMPLETE
    if state == TrialState.PRUNED:
        return _api_pb2.PRUNED
    if state == TrialState.FAIL:
        return _api_pb2.FAIL
    if state == TrialState.WAITING:
        return _api_pb2.WAITING
    raise ValueError(f"Unknown TrialState: {state}")


def _from_proto_trial_state(state: _api_pb2.global___TrialState) -> TrialState:
    if state == _api_pb2.RUNNING:
        return TrialState.RUNNING
    if state == _api_pb2.COMPLETE:
        return TrialState.COMPLETE
    if state == _api_pb2.PRUNED:
        return TrialState.PRUNED
    if state == _api_pb2.FAIL:
        return TrialState.FAIL
    if state == _api_pb2.WAITING:
        return TrialState.WAITING
    raise ValueError(f"Unknown _api_pb2.TrialState: {state}")


def _to_proto_frozen_trial(frozen_trial: FrozenTrial) -> _api_pb2.FrozenTrial:
    params = {}
    for key, value in frozen_trial.params.items():
        params[key] = frozen_trial.distributions[key].to_internal_repr(value)

    return _api_pb2.FrozenTrial(
        trial_id=frozen_trial._trial_id,
        number=frozen_trial.number,
        state=_to_proto_trial_state(frozen_trial.state),
        values=frozen_trial.values,
        datetime_start=(
            frozen_trial.datetime_start.strftime(DATETIME_FORMAT)
            if frozen_trial.datetime_start
            else ""
        ),
        datetime_complete=(
            frozen_trial.datetime_complete.strftime(DATETIME_FORMAT)
            if frozen_trial.datetime_complete
            else ""
        ),
        distributions={
            key: distribution_to_json(distribution)
            for key, distribution in frozen_trial.distributions.items()
        },
        params=params,
        user_attributes={key: json.dumps(value) for key, value in frozen_trial.user_attrs.items()},
        system_attributes={
            key: json.dumps(value) for key, value in frozen_trial.system_attrs.items()
        },
        intermediate_values={
            step: value for step, value in frozen_trial.intermediate_values.items()
        },
    )


def _from_proto_frozen_trial(frozen_trial: _api_pb2.FrozenTrial) -> FrozenTrial:
    datetime_start = (
        datetime.strptime(frozen_trial.datetime_start, DATETIME_FORMAT)
        if frozen_trial.datetime_start
        else None
    )
    datetime_complete = (
        datetime.strptime(frozen_trial.datetime_complete, DATETIME_FORMAT)
        if frozen_trial.datetime_complete
        else None
    )
    distributions = {
        key: json_to_distribution(value) for key, value in frozen_trial.distributions.items()
    }
    params = {}
    for key, value in frozen_trial.params.items():
        params[key] = distributions[key].to_external_repr(value)

    return FrozenTrial(
        trial_id=frozen_trial.trial_id,
        number=frozen_trial.number,
        state=_from_proto_trial_state(frozen_trial.state),
        value=None,
        values=frozen_trial.values if frozen_trial.values else None,
        datetime_start=datetime_start,
        datetime_complete=datetime_complete,
        params=params,
        distributions=distributions,
        user_attrs={key: json.loads(value) for key, value in frozen_trial.user_attributes.items()},
        system_attrs={
            key: json.loads(value) for key, value in frozen_trial.system_attributes.items()
        },
        intermediate_values={
            step: value for step, value in frozen_trial.intermediate_values.items()
        },
    )


def run_server(
    storage_url: str, host: str, port: int, thread_pool: ThreadPoolExecutor | None = None
) -> None:
    server = grpc.server(thread_pool or ThreadPoolExecutor(max_workers=10))
    add_StorageServiceServicer_to_server(OptunaStorageProxyService(storage_url), server)
    server.add_insecure_port(f"{host}:{port}")
    server.start()
    print(f"Server started at {host}:{port}")
    print("Listening...")
    server.wait_for_termination()
