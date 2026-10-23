---- MODULE LateReceipts ----
\* Formal model of the Media Edge playback-receipt handler when a generation is
\* replaced (a Voice Core cancel or the next reply, or a device stop).
\*
\* Code under model (services/media_edge):
\*   device_ws_uplink.go        handlePlaybackReceipt, logDroppedLateReceipt,
\*                              handleButtonStop (device stop clears the window)
\*   device_playback_ledger.go  devicePlaybackLedger.record
\*   bridge_runtime.go          VoiceCoreMediaRuntime.GenerationReplaced
\*                              VoiceCoreMediaRuntime.SendPlaybackProgress
\*   bridge_session_send.go     VoiceCoreSession.SendPlaybackProgress (fence check)
\*   device_ws_session.go       DeviceConnection.playbackActive/playbackFence,
\*                              clearPlaybackActive
\*   device_ws_downlink.go      generation.cancelled deliberately leaves the window
\*   bridge_events.go           VoiceCoreSession.current moves on the receive loop
\*   session_generation.go      a device stop moves the Edge Session fence before
\*                              SendStop moves Voice Core's fence
\*
\* The model tracks two independent generation authorities:
\*   coreGen  VoiceCoreSession.current, the fence SendPlaybackProgress checks
\*   edgeGen  the Media Edge Session.Generation fence
\* Either can move first in the real system: a Voice Core-initiated cancel moves
\* coreGen first and edgeGen later (bridge_runtime_events.go), a device button
\* stop moves edgeGen first and coreGen on the SendStop round trip
\* (session_generation.go then bridge_runtime.go), and a receipt can be handled
\* while the two are apart.  The handler steps are atomic actions on one read
\* goroutine; the authority advances interleave between them.
\*
\* The invariants read observer variables that describe what the receipt path is
\* supposed to have done (OracleReplaced for the true replacement, the receipt's
\* own type for the window it should own, coreGen at the accepted send),
\* independently of the guards under test.  Mutants change Replaced, one window
\* effect, or one guard only; the observers stay.
\*
\* Scope: one session, one stream epoch, one turn; only generation_id moves.
\* Device receipts may only name a fence whose downlink was sent.  The ledger's
\* transport anchors, sequence/sample arithmetic, parity and overflow checks are
\* abstracted to "this fence was sent"; sequence regression and terminal fences
\* are modelled.  Transport reconnects, Voice Core's own processing, a device
\* stop that Voice Core rejects because its fence already moved, and the
\* sub-statement gaps between a check and the next statement (sendSessionError,
\* the fence check inside SendPlaybackProgress versus its gRPC write) are out of
\* scope.
EXTENDS Naturals, FiniteSets, TLC

CONSTANT MaxReceipts

VARIABLES
  coreGen, edgeGen,
  anchored,
  hasReceipt, terminalReceipt, lastSeq,
  pending, used,
  status, cur, curOK, beginOracleReplaced,
  connOpen, windowActive, windowFence,
  lastCloseOracleReplaced,
  lastForwardAccepted, lastForwardAcceptedOracleReplaced,
  lastAction, beforeActive, beforeFence, beforeReceiptFence,
  lastDropReallyReplaced,
  forwardWindowCheck,
  terminalWindowCheck

F1 == [se |-> 1, turn |-> 1, gen |-> 1, tool |-> 0]
F2 == [se |-> 1, turn |-> 1, gen |-> 2, tool |-> 0]
Fences == {F1, F2}
ReceiptTypes == {"started", "progress", "ended", "error"}
ReceiptSet == [type: ReceiptTypes, fence: Fences, seq: 1..2]
NONE == [type |-> "none", fence |-> F1, seq |-> 0]

IsTerminal(t) == t = "ended" \/ t = "error"

Ahead(f, c) == c.gen > f.gen

\* What the code under test consults.  Mutants replace this definition only.
Replaced(f) == Ahead(f, coreGen) \/ Ahead(f, edgeGen)

\* What replacement really means, independent of the check being tested.  The
\* invariants read this, so a check that forgets one authority, or drops a live
\* receipt, is caught even when its own Replaced says otherwise.
OracleReplaced(f) == Ahead(f, coreGen) \/ Ahead(f, edgeGen)

vars == << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq,
          pending, used, status, cur, curOK, beginOracleReplaced,
          connOpen, windowActive, windowFence,
          lastCloseOracleReplaced, lastForwardAccepted,
          lastForwardAcceptedOracleReplaced,
          lastAction, beforeActive, beforeFence, beforeReceiptFence,
          lastDropReallyReplaced, forwardWindowCheck, terminalWindowCheck >>

Statuses == {"idle", "ledgerRefused", "terminalCheck", "terminalReplaced",
             "precheckCheck", "precheckReplaced", "openWindow", "forward",
             "forwardRefused"}

\* A drop starts here: record the window the handler leaves alone (or clears,
\* for a terminal receipt of the generation that opened it) and whether the
\* generation was really replaced, per the independent oracle.
MarkDrop(action) ==
  /\ lastAction' = action
  /\ beforeActive' = windowActive
  /\ beforeFence' = windowFence
  /\ beforeReceiptFence' = cur.fence
  /\ lastDropReallyReplaced' = OracleReplaced(cur.fence)
  /\ forwardWindowCheck' = [checked |-> FALSE, expectedActive |-> FALSE, expectedFence |-> F1]
  /\ terminalWindowCheck' = [checked |-> FALSE, wasOwn |-> FALSE]
  /\ lastForwardAccepted' = FALSE
  /\ lastForwardAcceptedOracleReplaced' = FALSE

MarkTerminalDrop ==
  /\ lastAction' = "terminalReplacedDrop"
  /\ beforeActive' = windowActive
  /\ beforeFence' = windowFence
  /\ beforeReceiptFence' = cur.fence
  /\ lastDropReallyReplaced' = OracleReplaced(cur.fence)
  /\ forwardWindowCheck' = [checked |-> FALSE, expectedActive |-> FALSE, expectedFence |-> F1]
  /\ terminalWindowCheck' = [checked |-> TRUE, wasOwn |-> (windowActive /\ windowFence = cur.fence)]
  /\ lastForwardAccepted' = FALSE
  /\ lastForwardAcceptedOracleReplaced' = FALSE

\* Any other step ends the previous observation window.
MarkIdle ==
  /\ lastAction' = "none"
  /\ lastDropReallyReplaced' = FALSE
  /\ forwardWindowCheck' = [checked |-> FALSE, expectedActive |-> FALSE, expectedFence |-> F1]
  /\ terminalWindowCheck' = [checked |-> FALSE, wasOwn |-> FALSE]
  /\ lastForwardAccepted' = FALSE
  /\ lastForwardAcceptedOracleReplaced' = FALSE
  /\ UNCHANGED << beforeActive, beforeFence, beforeReceiptFence >>

Init ==
  /\ coreGen = F1
  /\ edgeGen = F1
  /\ anchored = [f \in Fences |-> f = F1]
  /\ hasReceipt = [f \in Fences |-> FALSE]
  /\ terminalReceipt = [f \in Fences |-> FALSE]
  /\ lastSeq = [f \in Fences |-> 0]
  /\ pending = NONE
  /\ used = {}
  /\ status = "idle"
  /\ cur = [type |-> "started", fence |-> F1, seq |-> 1]
  /\ curOK = FALSE
  /\ beginOracleReplaced = FALSE
  /\ connOpen = TRUE
  /\ windowActive = FALSE
  /\ windowFence = F1
  /\ lastCloseOracleReplaced = FALSE
  /\ lastForwardAccepted = FALSE
  /\ lastForwardAcceptedOracleReplaced = FALSE
  /\ lastAction = "none"
  /\ beforeActive = FALSE
  /\ beforeFence = F1
  /\ beforeReceiptFence = F1
  /\ lastDropReallyReplaced = FALSE
  /\ forwardWindowCheck = [checked |-> FALSE, expectedActive |-> FALSE, expectedFence |-> F1]
  /\ terminalWindowCheck = [checked |-> FALSE, wasOwn |-> FALSE]

\* ledger.record: a terminal fence refuses every later receipt; otherwise the
\* received sequence must not move backwards.  The transport anchor check is
\* modelled by Enqueue below, which only lets the device report sent fences.
RecordOK(r) ==
  ~terminalReceipt[r.fence] /\ ~(hasReceipt[r.fence] /\ r.seq < lastSeq[r.fence])

Enqueue ==
  /\ status = "idle" /\ pending = NONE /\ connOpen
  /\ Cardinality(used) < MaxReceipts
  /\ \E r \in ReceiptSet \ used:
       /\ anchored[r.fence]
       /\ pending' = r
       /\ used' = used \cup {r}
       /\ MarkIdle
       /\ UNCHANGED << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt,
                       lastSeq, status, cur, curOK, beginOracleReplaced,
                       connOpen, windowActive, windowFence,
                       lastCloseOracleReplaced >>

\* ledger.record, then the handler picks the next check from the receipt type:
\* a terminal receipt goes through GenerationReplaced twice (terminal branch,
\* then precheck), a progress/started receipt once (precheck).
Begin ==
  /\ status = "idle" /\ pending /= NONE
  /\ LET r == pending IN
       /\ cur' = r
       /\ curOK' = RecordOK(r)
       /\ IF RecordOK(r)
          THEN /\ hasReceipt' = [hasReceipt EXCEPT ![r.fence] = TRUE]
               /\ lastSeq' = [lastSeq EXCEPT ![r.fence] = r.seq]
               /\ terminalReceipt' = [terminalReceipt EXCEPT ![r.fence] = IsTerminal(r.type)]
          ELSE /\ UNCHANGED << hasReceipt, terminalReceipt, lastSeq >>
       /\ status' =
            IF ~RecordOK(r) THEN "ledgerRefused"
            ELSE IF IsTerminal(r.type) THEN "terminalCheck"
            ELSE "precheckCheck"
       /\ beginOracleReplaced' = OracleReplaced(r.fence)
       /\ pending' = NONE
       /\ MarkIdle
       /\ UNCHANGED << coreGen, edgeGen, anchored, used, connOpen, windowActive,
                       windowFence, lastCloseOracleReplaced >>

\* First terminal check: a replaced generation's ended/error is the device
\* acknowledging the flush; it ends that generation's window (2026-09-29 fix).
TerminalCheck ==
  /\ status = "terminalCheck"
  /\ status' = IF Replaced(cur.fence) THEN "terminalReplaced" ELSE "precheckCheck"
  /\ MarkIdle
  /\ UNCHANGED << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq,
                  pending, used, cur, curOK, beginOracleReplaced, connOpen,
                  windowActive, windowFence, lastCloseOracleReplaced >>

\* Precheck: a replaced receipt is dropped before it touches the window.
\* A terminal receipt still ends its own window (2026-10-02 fix plus F1).
PrecheckCheck ==
  /\ status = "precheckCheck"
  /\ status' = IF Replaced(cur.fence) THEN "precheckReplaced" ELSE "openWindow"
  /\ MarkIdle
  /\ UNCHANGED << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq,
                  pending, used, cur, curOK, beginOracleReplaced, connOpen,
                  windowActive, windowFence, lastCloseOracleReplaced >>

\* Voice Core validates a cancel / starts the next generation on its receive
\* loop: its own session fence moves first.
CoreAdvance ==
  /\ coreGen = F1
  /\ coreGen' = F2
  /\ MarkIdle
  /\ UNCHANGED << edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq, pending,
                  used, status, cur, curOK, beginOracleReplaced, connOpen,
                  windowActive, windowFence, lastCloseOracleReplaced >>

\* The Edge Session applies a fence Voice Core already names (bridge event
\* goroutine); this is the gap the 2026-10-02 fix tests with the fake core.
EdgeApplyCore ==
  /\ coreGen = F2 /\ edgeGen = F1
  /\ edgeGen' = F2
  /\ MarkIdle
  /\ UNCHANGED << coreGen, anchored, hasReceipt, terminalReceipt, lastSeq, pending,
                  used, status, cur, curOK, beginOracleReplaced, connOpen,
                  windowActive, windowFence, lastCloseOracleReplaced >>

\* A successful device button stop: it runs on the same read goroutine, so it
\* only happens between receipts, and only while Voice Core does not already
\* name a newer fence.  The Edge Session fence moves before SendStop moves
\* Voice Core's, and the stop closes the window the device was rendering
\* (handleButtonStop -> clearPlaybackActive).  A stale stop that Voice Core
\* rejects is the separate open button.stop path, not modelled here.
DeviceStopEdgeFirst ==
  /\ status = "idle" /\ pending = NONE
  /\ coreGen = F1 /\ edgeGen = F1
  /\ edgeGen' = F2
  /\ windowActive' = IF windowActive /\ windowFence = edgeGen THEN FALSE ELSE windowActive
  /\ MarkIdle
  /\ UNCHANGED << coreGen, anchored, hasReceipt, terminalReceipt, lastSeq, pending,
                  used, status, cur, curOK, beginOracleReplaced, connOpen,
                  windowFence, lastCloseOracleReplaced >>

\* The Edge has sent downlink for the replacement generation (both fences have
\* it), so its receipts now pass the transport anchor check.
SendDownlink ==
  /\ coreGen = F2 /\ edgeGen = F2 /\ ~anchored[F2]
  /\ anchored' = [anchored EXCEPT ![F2] = TRUE]
  /\ MarkIdle
  /\ UNCHANGED << coreGen, edgeGen, hasReceipt, terminalReceipt, lastSeq, pending,
                  used, status, cur, curOK, beginOracleReplaced, connOpen,
                  windowActive, windowFence, lastCloseOracleReplaced >>

\* ledger refused the receipt, and its generation is replaced: drop it.  No
\* session.error or close.  A terminal receipt still ends the window it owns;
\* non-terminal receipts leave the current window untouched (2026-09-30).
DropRefused ==
  /\ status = "ledgerRefused"
  /\ Replaced(cur.fence)
  /\ status' = "idle"
  /\ MarkDrop("drop")
  /\ IF IsTerminal(cur.type) /\ windowActive /\ windowFence = cur.fence
     THEN /\ windowActive' = FALSE
          /\ UNCHANGED << windowFence >>
     ELSE /\ UNCHANGED << windowActive, windowFence >>
  /\ UNCHANGED << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq,
                  pending, used, cur, curOK, beginOracleReplaced, connOpen,
                  lastCloseOracleReplaced >>

\* ledger refused the receipt for a live generation: the control handler fails,
\* the read loop closes the connection.
CloseRefused ==
  /\ status = "ledgerRefused"
  /\ ~Replaced(cur.fence)
  /\ connOpen' = FALSE
  /\ status' = "idle"
  /\ lastCloseOracleReplaced' = OracleReplaced(cur.fence)
  /\ MarkIdle
  /\ UNCHANGED << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq,
                  pending, used, cur, curOK, beginOracleReplaced, windowActive,
                  windowFence >>

\* A terminal receipt for a replaced generation ends the window it opened and
\* only that one (2026-09-29 fix).
DropTerminalReplaced ==
  /\ status = "terminalReplaced"
  /\ LET wasOwn == windowActive /\ windowFence = cur.fence IN
       /\ IF wasOwn
          THEN /\ windowActive' = FALSE
               /\ UNCHANGED << windowFence >>
          ELSE /\ UNCHANGED << windowActive, windowFence >>
  /\ status' = "idle"
  /\ MarkTerminalDrop
  /\ UNCHANGED << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq,
                  pending, used, cur, curOK, beginOracleReplaced, connOpen,
                  lastCloseOracleReplaced >>

\* A replaced non-terminal receipt must not be forwarded or disturb whatever
\* window is open now; a terminal receipt ends its own window (2026-10-02 plus F1).
DropPrecheck ==
  /\ status = "precheckReplaced"
  /\ status' = "idle"
  /\ MarkDrop("drop")
  /\ IF IsTerminal(cur.type) /\ windowActive /\ windowFence = cur.fence
     THEN /\ windowActive' = FALSE
          /\ UNCHANGED << windowFence >>
     ELSE /\ UNCHANGED << windowActive, windowFence >>
  /\ UNCHANGED << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq,
                  pending, used, cur, curOK, beginOracleReplaced, connOpen,
                  lastCloseOracleReplaced >>

\* Accepted receipt opens or closes the playback window exactly as the device
\* reported; a terminal receipt for a live generation is its own authoritative end.
OpenWindow ==
  /\ status = "openWindow"
  /\ status' = "forward"
  /\ IF cur.type = "started" \/ cur.type = "progress"
     THEN /\ windowActive' = TRUE
          /\ windowFence' = cur.fence
     ELSE /\ windowActive' = FALSE
          /\ UNCHANGED << windowFence >>
  /\ MarkIdle
  /\ UNCHANGED << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq,
                  pending, used, cur, curOK, beginOracleReplaced, connOpen,
                  lastCloseOracleReplaced >>

\* Voice Core accepts the progress only when its session fence still names the
\* receipt's generation (bridge_session_send.go SendPlaybackProgress); the gRPC
\* write can also fail on its own.  Both land in the forward-error branch.  The
\* fence check and the write are one step here; see the scope note.
Forward ==
  /\ status = "forward"
  /\ \/ ( /\ cur.fence = coreGen
           /\ status' = "idle"
           /\ lastForwardAccepted' = TRUE
           /\ lastForwardAcceptedOracleReplaced' = beginOracleReplaced )
      \/ ( /\ status' = "forwardRefused"
           /\ lastForwardAccepted' = FALSE
           /\ lastForwardAcceptedOracleReplaced' = FALSE )
  /\ lastAction' = "none"
  /\ lastDropReallyReplaced' = FALSE
  /\ forwardWindowCheck' =
       [checked |-> TRUE,
        expectedActive |-> (cur.type = "started" \/ cur.type = "progress"),
        expectedFence |-> cur.fence]
  /\ terminalWindowCheck' = [checked |-> FALSE, wasOwn |-> FALSE]
  /\ UNCHANGED << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq,
                  pending, used, cur, curOK, beginOracleReplaced, connOpen,
                  windowActive, windowFence, lastCloseOracleReplaced,
                  beforeActive, beforeFence, beforeReceiptFence >>

\* The replacement landed between the precheck and the send: drop, and close
\* the window this receipt just opened for the now-dead generation (2026-10-02
\* fix, stage=forward).
DropAfterForwardError ==
  /\ status = "forwardRefused" /\ Replaced(cur.fence)
  /\ LET wasOwn == windowActive /\ windowFence = cur.fence IN
       /\ IF wasOwn
          THEN /\ windowActive' = FALSE
               /\ UNCHANGED << windowFence >>
          ELSE /\ UNCHANGED << windowActive, windowFence >>
  /\ status' = "idle"
  /\ MarkDrop("forwardErrorDrop")
  /\ UNCHANGED << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq,
                  pending, used, cur, curOK, beginOracleReplaced, connOpen,
                  lastCloseOracleReplaced >>

\* The send failed for a live generation: fail closed as before.
CloseAfterForwardError ==
  /\ status = "forwardRefused" /\ ~Replaced(cur.fence)
  /\ connOpen' = FALSE
  /\ status' = "idle"
  /\ lastCloseOracleReplaced' = OracleReplaced(cur.fence)
  /\ MarkIdle
  /\ UNCHANGED << coreGen, edgeGen, anchored, hasReceipt, terminalReceipt, lastSeq,
                  pending, used, cur, curOK, beginOracleReplaced, windowActive,
                  windowFence >>

Next ==
  \/ Enqueue
  \/ Begin
  \/ TerminalCheck
  \/ PrecheckCheck
  \/ CoreAdvance
  \/ EdgeApplyCore
  \/ DeviceStopEdgeFirst
  \/ SendDownlink
  \/ DropRefused
  \/ CloseRefused
  \/ DropTerminalReplaced
  \/ DropPrecheck
  \/ OpenWindow
  \/ Forward
  \/ DropAfterForwardError
  \/ CloseAfterForwardError

Spec == Init /\ [][Next]_vars

\* --- Invariants -------------------------------------------------------------

TypeOK ==
  /\ coreGen \in Fences /\ edgeGen \in Fences
  /\ anchored \in [Fences -> BOOLEAN]
  /\ hasReceipt \in [Fences -> BOOLEAN]
  /\ terminalReceipt \in [Fences -> BOOLEAN]
  /\ lastSeq \in [Fences -> 0..2]
  /\ pending \in ReceiptSet \cup {NONE}
  /\ used \subseteq ReceiptSet
  /\ status \in Statuses
  /\ cur \in ReceiptSet
  /\ curOK \in BOOLEAN
  /\ beginOracleReplaced \in BOOLEAN
  /\ connOpen \in BOOLEAN
  /\ windowActive \in BOOLEAN
  /\ windowFence \in Fences
  /\ lastCloseOracleReplaced \in BOOLEAN
  /\ lastForwardAccepted \in BOOLEAN
  /\ lastForwardAcceptedOracleReplaced \in BOOLEAN
  /\ lastAction \in {"none", "drop", "terminalReplacedDrop", "forwardErrorDrop"}
  /\ beforeActive \in BOOLEAN
  /\ beforeFence \in Fences
  /\ beforeReceiptFence \in Fences
  /\ lastDropReallyReplaced \in BOOLEAN
  /\ forwardWindowCheck \in [checked: BOOLEAN, expectedActive: BOOLEAN, expectedFence: Fences]
  /\ terminalWindowCheck \in [checked: BOOLEAN, wasOwn: BOOLEAN]

\* A close decision is never made for a generation that is replaced.
NoCloseForReplaced == lastCloseOracleReplaced = FALSE

\* An accepted send for a generation that was already replaced when the handler
\* started processing it never happens.  (A refused send is not a forward.)
NoForwardForReplacedAtBegin == lastForwardAcceptedOracleReplaced = FALSE

\* An accepted send only happens while Voice Core's fence still names the
\* receipt's generation (the SendPlaybackProgress check).
ForwardAcceptedAtCoreFence == lastForwardAccepted => cur.fence = coreGen

\* A late receipt is dropped only when its generation was really replaced.
DropsOnlyForReplaced == lastAction = "none" \/ lastDropReallyReplaced

\* Dropping a late receipt never changes a playback window that belongs to a
\* different generation.
DropKeepsForeignWindow ==
  lastAction = "drop" =>
    (beforeActive /\ beforeFence /= beforeReceiptFence =>
       (windowActive = beforeActive /\ windowFence = beforeFence))

\* A replaced terminal receipt clears only the window its own generation opened,
\* and it does clear that window.
TerminalClearOnlyOwnWindow ==
  terminalWindowCheck.checked /\ ~terminalWindowCheck.wasOwn =>
    (windowActive = beforeActive /\ windowFence = beforeFence)

TerminalClearEndsOwnWindow ==
  terminalWindowCheck.checked /\ terminalWindowCheck.wasOwn => ~windowActive

\* When a send refused after a replacement and the receipt owns the open
\* window, that window is closed again.
ForwardDropClosesOwnWindow ==
  lastAction = "forwardErrorDrop" =>
    (beforeActive /\ beforeFence = beforeReceiptFence => ~windowActive)

\* A forwarded receipt leaves the playback window exactly as its type says:
\* started/progress open it for that fence, ended/error close it.
WindowMatchesForward ==
  forwardWindowCheck.checked =>
    (windowActive = forwardWindowCheck.expectedActive /\
     (forwardWindowCheck.expectedActive =>
        windowFence = forwardWindowCheck.expectedFence))

\* F1 regression guard: both early drop paths must end a replaced terminal
\* receipt's own playback window.
TerminalReceiptEndsOwnWindow ==
  lastAction = "drop" =>
    (IsTerminal(cur.type) /\ beforeActive /\
     beforeFence = beforeReceiptFence => ~windowActive)

====
