"use client";

import { useEffect, useState } from "react";
import {
  ApiError,
  AvailableNumber,
  Carrier,
  CarrierProvider,
  PhoneNumber,
  createCarrier,
  createPhoneNumber,
  listCarriers,
  listPhoneNumbers,
  purchaseNumber,
  searchAvailableNumbers,
  updatePhoneNumber,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

const PROVIDER_LABEL: Record<CarrierProvider, string> = {
  twilio: "Twilio",
  plivo: "Plivo",
  vonage: "Vonage",
};

export function SipPanel({ tenantId, agentId }: { tenantId: string; agentId: string }) {
  const [carriers, setCarriers] = useState<Carrier[]>([]);
  const [numbers, setNumbers] = useState<PhoneNumber[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [addingCarrier, setAddingCarrier] = useState(false);
  const [carrierName, setCarrierName] = useState("");
  const [carrierProvider, setCarrierProvider] = useState<CarrierProvider>("twilio");
  const [carrierAuthId, setCarrierAuthId] = useState("");
  const [carrierAuthTokenRef, setCarrierAuthTokenRef] = useState("");
  const [savingCarrier, setSavingCarrier] = useState(false);
  const [carrierError, setCarrierError] = useState<string | null>(null);

  const [buying, setBuying] = useState(false);
  const [buyCarrierId, setBuyCarrierId] = useState("");
  const [buyCountry, setBuyCountry] = useState("US");
  const [buyAreaCode, setBuyAreaCode] = useState("");
  const [searchResults, setSearchResults] = useState<AvailableNumber[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [purchasingNumber, setPurchasingNumber] = useState<string | null>(null);
  const [buyError, setBuyError] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const [c, n] = await Promise.all([listCarriers(tenantId), listPhoneNumbers(tenantId)]);
      setCarriers(c);
      setNumbers(n);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, agentId]);

  const assignedNumbers = numbers.filter((n) => n.agent_id === agentId);
  const unassignedNumbers = numbers.filter((n) => !n.agent_id);

  const openAddCarrier = () => {
    setCarrierName("");
    setCarrierProvider("twilio");
    setCarrierAuthId("");
    setCarrierAuthTokenRef("");
    setCarrierError(null);
    setAddingCarrier(true);
  };

  const handleSaveCarrier = async () => {
    setSavingCarrier(true);
    setCarrierError(null);
    try {
      await createCarrier(tenantId, {
        name: carrierName,
        provider: carrierProvider,
        auth_id: carrierAuthId || undefined,
        auth_token_ref: carrierAuthTokenRef || undefined,
      });
      setAddingCarrier(false);
      await refresh();
    } catch (e) {
      setCarrierError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSavingCarrier(false);
    }
  };

  const openBuy = () => {
    setBuyCarrierId(carriers[0]?.id ?? "");
    setBuyCountry("US");
    setBuyAreaCode("");
    setSearchResults(null);
    setBuyError(null);
    setBuying(true);
  };

  const handleSearch = async () => {
    setSearching(true);
    setBuyError(null);
    setSearchResults(null);
    try {
      const results = await searchAvailableNumbers(tenantId, {
        carrier_id: buyCarrierId,
        country: buyCountry,
        area_code: buyAreaCode || undefined,
        limit: 10,
      });
      setSearchResults(results);
    } catch (e) {
      setBuyError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSearching(false);
    }
  };

  const handlePurchase = async (phoneNumber: string) => {
    if (!confirm(`Purchase ${phoneNumber}? This charges your carrier account for real.`)) return;
    setPurchasingNumber(phoneNumber);
    setBuyError(null);
    try {
      await purchaseNumber(tenantId, { carrier_id: buyCarrierId, phone_number: phoneNumber });
      // Config Service owns DID->agent routing (phone_numbers) — DID Service
      // only owns the carrier purchase itself, per architecture principle #7.
      await createPhoneNumber(tenantId, {
        did: phoneNumber,
        agent_id: agentId,
        carrier_id: buyCarrierId,
        status: "active",
      });
      setBuying(false);
      await refresh();
    } catch (e) {
      setBuyError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setPurchasingNumber(null);
    }
  };

  const handleAssignExisting = async (phoneNumberId: string) => {
    if (!phoneNumberId) return;
    try {
      await updatePhoneNumber(phoneNumberId, { agent_id: agentId });
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleUnassign = async (phoneNumberId: string) => {
    try {
      await updatePhoneNumber(phoneNumberId, { agent_id: null });
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  if (loading) return <div className="empty-state">Loading…</div>;

  return (
    <div className="cols">
      <div className="col-main">
        {error && <div className="error-banner">{error}</div>}

        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Trunk Provider</div>
            <div className="card-sub">carriers this tenant can buy numbers from</div>
            <button className="btn btn-primary btn-sm" style={{ marginLeft: "auto" }} onClick={openAddCarrier}>
              + Add Carrier
            </button>
          </div>

          {carriers.map((c) => (
            <div key={c.id} className="kb-row">
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 500 }}>{c.name}</div>
                <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>{c.auth_id ?? "no auth_id set"}</div>
              </div>
              <span className="badge indigo">{PROVIDER_LABEL[c.provider]}</span>
            </div>
          ))}

          {carriers.length === 0 && <div className="empty-state">No carriers configured yet.</div>}
        </div>

        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-hdr">
            <div className="card-title">Assigned Number</div>
            <div className="card-sub">which DID routes calls to this agent</div>
            <button
              className="btn btn-primary btn-sm"
              style={{ marginLeft: "auto" }}
              onClick={openBuy}
              disabled={carriers.length === 0}
              title={carriers.length === 0 ? "Add a carrier first" : "Buy a new number"}
            >
              + Buy a Number
            </button>
          </div>

          {assignedNumbers.map((n) => (
            <div key={n.id} className="kb-row">
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 500 }}>{n.did}</div>
                <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>Registered · {n.status}</div>
              </div>
              <span className={`badge ${n.status === "active" ? "green" : n.status === "suspended" ? "red" : "gray"}`}>
                {n.status}
              </span>
              <button className="btn btn-ghost btn-sm" onClick={() => handleUnassign(n.id)}>
                Unassign
              </button>
            </div>
          ))}

          {assignedNumbers.length === 0 && (
            <>
              <div className="empty-state">No number assigned to this agent.</div>
              {unassignedNumbers.length > 0 && (
                <div className="form-group">
                  <label className="form-label">Assign an existing unassigned number</label>
                  <select className="form-input" defaultValue="" onChange={(e) => handleAssignExisting(e.target.value)}>
                    <option value="" disabled>
                      Choose a number…
                    </option>
                    {unassignedNumbers.map((n) => (
                      <option key={n.id} value={n.id}>
                        {n.did}
                      </option>
                    ))}
                  </select>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      <Modal
        open={addingCarrier}
        title="Add Carrier"
        onClose={() => setAddingCarrier(false)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setAddingCarrier(false)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={handleSaveCarrier} disabled={savingCarrier || !carrierName}>
              {savingCarrier ? "Adding…" : "Add Carrier"}
            </button>
          </>
        }
      >
        {carrierError && <div className="error-banner">{carrierError}</div>}
        <div className="form-group">
          <label className="form-label">
            Name <span className="required">*</span>
          </label>
          <input className="form-input" value={carrierName} onChange={(e) => setCarrierName(e.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label">Provider</label>
          <select
            className="form-input"
            value={carrierProvider}
            onChange={(e) => setCarrierProvider(e.target.value as CarrierProvider)}
          >
            <option value="twilio">Twilio</option>
            <option value="plivo">Plivo</option>
            <option value="vonage">Vonage</option>
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">
            Account SID / Auth ID <span className="hint">e.g. AC... for Twilio, Auth ID for Plivo</span>
          </label>
          <input className="form-input" value={carrierAuthId} onChange={(e) => setCarrierAuthId(e.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label">
            Auth Token Reference <span className="hint">e.g. env:TWILIO_AUTH_TOKEN — never a raw secret</span>
          </label>
          <input
            className="form-input"
            style={{ fontFamily: "var(--mono)" }}
            value={carrierAuthTokenRef}
            onChange={(e) => setCarrierAuthTokenRef(e.target.value)}
            placeholder="env:TWILIO_AUTH_TOKEN"
          />
        </div>
      </Modal>

      <Modal
        open={buying}
        title="Buy a Number"
        onClose={() => setBuying(false)}
        footer={
          <button className="btn btn-ghost btn-sm" onClick={() => setBuying(false)}>
            Close
          </button>
        }
      >
        {buyError && <div className="error-banner">{buyError}</div>}
        <div className="form-group">
          <label className="form-label">Carrier</label>
          <select className="form-input" value={buyCarrierId} onChange={(e) => setBuyCarrierId(e.target.value)}>
            {carriers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({PROVIDER_LABEL[c.provider]})
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">Country</label>
          <input className="form-input" value={buyCountry} onChange={(e) => setBuyCountry(e.target.value.toUpperCase())} maxLength={2} />
        </div>
        <div className="form-group">
          <label className="form-label">Area Code (optional)</label>
          <input className="form-input" value={buyAreaCode} onChange={(e) => setBuyAreaCode(e.target.value)} placeholder="415" />
        </div>
        <button className="btn btn-primary btn-sm" onClick={handleSearch} disabled={searching || !buyCarrierId}>
          {searching ? "Searching…" : "Search"}
        </button>

        {searchResults !== null && (
          <div style={{ marginTop: 16 }}>
            {searchResults.length === 0 && <div className="empty-state">No numbers found — try a different area code.</div>}
            {searchResults.map((r) => (
              <div key={r.phone_number} className="kb-row">
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 500 }}>{r.phone_number}</div>
                  <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                    {r.region ?? "—"} · {r.capabilities.join(", ")}
                  </div>
                </div>
                <button
                  className="btn btn-primary btn-sm"
                  onClick={() => handlePurchase(r.phone_number)}
                  disabled={purchasingNumber !== null}
                >
                  {purchasingNumber === r.phone_number ? "Purchasing…" : "Purchase"}
                </button>
              </div>
            ))}
          </div>
        )}
      </Modal>
    </div>
  );
}
