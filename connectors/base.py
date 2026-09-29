from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
@dataclass(frozen=True)
class ConnectorAction:
    id:str; label:str; risk:str; description:str; parameters:dict[str,Any]=field(default_factory=dict)
@dataclass(frozen=True)
class ConnectorSpec:
    id:str; name:str; category:str; description:str; auth_type:str; scopes:tuple[str,...]; actions:tuple[ConnectorAction,...]; enabled_by_default:bool=True
class ConnectorAdapter:
    spec:ConnectorSpec
    def authorization_url(self,state,redirect_uri): raise NotImplementedError
    def exchange_code(self,code,redirect_uri): raise NotImplementedError
    def refresh(self,secret): return None
    def test_connection(self,secret): raise NotImplementedError
    def revoke(self,secret): return False
    def list_resources(self,secret): raise NotImplementedError
    def perform(self,secret,action,params): raise NotImplementedError
