"""Owner-chosen delegatable self-revision password, stored only as scrypt hash.

Possession is the chosen authorization policy; it makes no claim that a human
person is currently present. No password enters persistence, errors or logs.
"""
import hashlib
import hmac
import json
from pathlib import Path
import threading
import time
import uuid

from runtime.execution_binding import (
    ExecutionClaim, assert_bound_execution, canonical_hash, current_execution_claim,
)

FORMAT = 'st-self-password-scrypt/1'

def encode_password(password, *, salt):
    if (not isinstance(password, str) or not 1 <= len(password) <= 1024
            or not isinstance(salt, bytes) or len(salt) != 16):
        raise ValueError('password_input_invalid')
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1, dklen=32)
    return {'format': FORMAT, 'salt_hex': salt.hex(), 'digest_hex': digest.hex()}

class SelfPasswordAuthority:
    def __init__(self, path, *, clock=time.monotonic, ttl=900):
        self.path = Path(path) if path else None
        self.clock, self.ttl = clock, ttl
        self.lock = threading.Lock()
        self.allowed_until = 0.0
        self.failed_attempts = 0
        self.blocked_until = 0.0
        self.authorized_hash = None

    def _read(self):
        if self.path is None or self.path.is_symlink() or not self.path.is_file():
            raise ValueError('self_password_not_configured')
        raw = self.path.read_bytes()
        if len(raw) > 1024:
            raise ValueError('self_password_configuration_invalid')
        value = json.loads(raw)
        if set(value) != {'format','salt_hex','digest_hex'} or value['format'] != FORMAT:
            raise ValueError('self_password_configuration_invalid')
        if len(value['salt_hex']) != 32 or len(value['digest_hex']) != 64:
            raise ValueError('self_password_configuration_invalid')
        salt, digest = bytes.fromhex(value['salt_hex']), bytes.fromhex(value['digest_hex'])
        return salt, digest, hashlib.sha256(raw).digest()

    def authorize(self, password):
        with self.lock:
            now = self.clock()
            if now < self.blocked_until:
                return {'decision':'reject', 'reason_code':'self_password_retry_later', 'state_changed':False}
            try:
                salt, expected, identity = self._read()
                valid_shape = isinstance(password, str) and 1 <= len(password) <= 1024
                actual = hashlib.scrypt((password if valid_shape else '').encode('utf-8'),
                                        salt=salt, n=16384, r=8, p=1, dklen=32)
                matched = valid_shape and hmac.compare_digest(actual, expected)
            except (OSError, ValueError, TypeError, KeyError):
                return {'decision':'reject', 'reason_code':'self_password_not_configured', 'state_changed':False}
            if not matched:
                self.failed_attempts += 1
                if self.failed_attempts >= 5:
                    self.blocked_until = now + 60
                    self.failed_attempts = 0
                return {'decision':'reject', 'reason_code':'self_password_invalid', 'state_changed':False}
            self.failed_attempts = 0
            self.allowed_until, self.authorized_hash = now + self.ttl, identity
            return {'decision':'authorized', 'authorization_basis':'deployment_password_possession',
                    'expires_in_seconds':self.ttl, 'scope':'self_revision', 'memory_changed':False}

    def allowed(self):
        with self.lock:
            try:
                _, _, identity = self._read()
            except (OSError, ValueError, TypeError, KeyError):
                return False
            return (self.clock() < self.allowed_until and self.authorized_hash is not None
                    and hmac.compare_digest(identity, self.authorized_hash))

    def revoke(self):
        with self.lock:
            self.allowed_until = 0.0
            self.authorized_hash = None

    def issue(self, password, *, onboarding, owner_id, model_id, client_principal):
        result = self.authorize(password)
        if result['decision'] != 'authorized':
            return result
        try:
            grant = onboarding.issue_direct_grant(
                owner_id=owner_id, model_id=model_id,
                actor_id='deployment-password-holder', client_principal=client_principal,
                request_id='password-' + uuid.uuid4().hex, requested_scopes=['self_revision'],
                authorization_basis='deployment_password_possession',
            )
            if not isinstance(grant.get('grant_ref'), str) or not grant['grant_ref']:
                raise ValueError('self_authorization_receipt_unavailable')
        except Exception:
            # Authorization is revoked if its matching direct receipt failed.
            self.revoke()
            return {'decision':'reject', 'reason_code':'self_authorization_receipt_unavailable',
                    'memory_changed':False}
        return {**result, 'grant_ref':grant.get('grant_ref'),
                'direct_next_step':'官方直连可用 grant_ref 调用 stbrain_open_direct；网关连接继续原有自我修改工具即可。',
                'message':'已授权本实例的模块一修改；普通模块可读取，新增和修改在模块一首次完成并激活后开放。'}

def install_self_password_guard(mcp, authority, *, execution_store, onboarding, owner_id, model_id):
    """Gateway claims or password-backed direct contexts authorize self writes.

    Install BEFORE the execution guard, so that execution validation is the outer
    dispatcher. A caller-provided ref, model name or context flag is never proof.
    The business layer still enforces the manual-open and self-revision stages.
    """
    manager = mcp._tool_manager
    if getattr(manager, '_st_self_password_guard', False):
        raise RuntimeError('self_password_guard_already_installed')
    manager._st_self_password_guard = True
    original = manager.call_tool

    def gateway_authorized(name, arguments, claim):
        if (execution_store is None or not isinstance(claim, ExecutionClaim)
                or (claim.owner_id, claim.model_id, claim.tool_name, claim.database, claim.deployment_epoch)
                != (owner_id, model_id, name, execution_store.database, execution_store.deployment_epoch)):
            return False
        try:
            with execution_store._connect() as connection:
                assert_bound_execution(connection)
                wake = execution_store._wake(connection, owner_id, model_id, claim.wake_id)
                if wake['source_kind'] == 'human_attested_direct':
                    return False
                row = connection.execute(
                    'SELECT c.batch_id,c.call_id,c.tool_name,c.arguments_hash,b.status '
                    'FROM brain_execution_calls c JOIN brain_execution_batches b ON c.batch_id=b.batch_id '
                    'WHERE c.ref_hash=? AND c.owner_id=? AND c.model_id=?',
                    (hashlib.sha256(claim.execution_ref.encode()).hexdigest(), owner_id, model_id),
                ).fetchone()
                return row is not None and tuple(row) == (
                    claim.batch_id, claim.call_id, name, canonical_hash(arguments), 'active',
                )
        except Exception:
            # Missing/stale registry proof fails closed; no capability, argument,
            # configuration path or exception text is copied into the response.
            return False

    def direct_authorized(arguments):
        ref = arguments.get('write_context_ref')
        if not isinstance(ref, str) or not ref.strip():
            return False
        try:
            binding = onboarding.current_open_write_context(
                owner_id=owner_id, model_id=model_id, write_context_ref=ref,
                required_scope='self_revision',
            )
            return (binding.get('write_context_available') is True
                    and binding.get('context_mode') == 'human_attested_direct'
                    and 'self_revision' in binding.get('authorized_scopes', ()))
        except Exception:
            return False

    async def guarded(name, arguments, context=None, convert_result=False):
        if name in {'submit_self_model_candidate','activate_self_model_candidate'}:
            claim = current_execution_claim()
            if claim is not None:
                allowed = gateway_authorized(name, arguments, claim)
                reason = 'execution_binding_invalid_or_finished'
                next_action = '从已连接网关的新消息重新调用，取得服务端核验的本轮工具绑定。'
            elif not authority.allowed():
                allowed = False
                reason = 'self_password_required'
                next_action = '直连修改模块一需先调用 authorize_self_model，再用 grant_ref 调用 stbrain_open_direct。'
            else:
                allowed = direct_authorized(arguments)
                reason = 'direct_grant_required'
                next_action = '用本次密码授权返回的 grant_ref 调用 stbrain_open_direct，再使用其模块一修改上下文。'
            if not allowed:
                result = {'decision':'reject', 'reason_code':reason, 'state_changed':False,
                          'next_action':next_action}
                tool = manager.get_tool(name)
                return tool.fn_metadata.convert_result(result) if convert_result else result
        return await original(name, arguments, context=context, convert_result=convert_result)
    manager.call_tool = guarded
