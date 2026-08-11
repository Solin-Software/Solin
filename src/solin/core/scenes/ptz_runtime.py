"""Composition root for the default PTZ adapters and bounded executor."""

from __future__ import annotations

from dataclasses import dataclass

from solin.core.scenes.model import PtzProtocol, new_identity
from solin.core.scenes.ptz import (
    PtzCredentialError,
    PtzCredentialVault,
    PtzExecutor,
)
from solin.core.scenes.ptz_credentials import (
    UnavailablePtzCredentialVault,
    create_system_ptz_credential_vault,
)
from solin.core.scenes.ptz_onvif import AiohttpOnvifTransport, OnvifPtzAdapter
from solin.core.scenes.ptz_visca import (
    PyserialViscaSerialTransport,
    SocketViscaIpTransport,
    ViscaIpPtzAdapter,
    ViscaSerialPtzAdapter,
)


@dataclass(frozen=True, slots=True)
class PtzRuntimeServices:
    credentials: PtzCredentialVault
    executor: PtzExecutor


def create_ptz_runtime_services(profile_id: str) -> PtzRuntimeServices:
    try:
        credentials: PtzCredentialVault = create_system_ptz_credential_vault(profile_id)
    except PtzCredentialError as error:
        credentials = UnavailablePtzCredentialVault(error.error_code)
    executor = PtzExecutor(
        {
            PtzProtocol.ONVIF: OnvifPtzAdapter(
                transport=AiohttpOnvifTransport(),
                credentials=credentials,
            ),
            PtzProtocol.VISCA_IP: ViscaIpPtzAdapter(
                transport=SocketViscaIpTransport()
            ),
            PtzProtocol.VISCA_SERIAL: ViscaSerialPtzAdapter(
                transport=PyserialViscaSerialTransport()
            ),
        },
        request_id_factory=new_identity,
    )
    return PtzRuntimeServices(credentials, executor)
